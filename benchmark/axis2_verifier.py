"""
Axis 2 verifier: detect unintended DB writes via row-level diff against a
golden replay.

Axis 1 (the existing database_state verifier) answers "did the required things
happen?" — it checks specific SQL predicates against the agent's final DB.
Axis 2 complements that by answering "did anything *extra* happen?" — it
seeds a second, parallel DB from the same seed, replays the canonical
"correct" write sequence against it, and diffs the agent's final DB against
that golden DB row-by-row.

Pipeline (per gym, per run):
  1. The executor seeds the agent's DB normally (already happens today).
  2. If compute_axis_2 is true, it also seeds a SECOND "golden" DB from the
     same seed file and tracks it for cleanup.
  3. The agent runs; mutates agent_db.
  4. This verifier replays each golden_tool_calls entry against the golden DB
     by re-using the existing MCPClient with its database_id kwarg pointed
     at the golden DB.
  5. snapshot() pulls SELECT * FROM <table> for both DBs.
  6. diff() compares rows by primary key (discovered server-side from
     sqlite_master + PRAGMA table_info, since seed dumps are INSERT-only).
  7. Emits an axis_2_unintended_changes block on the run dict.

Output block shape (attached as a top-level sibling of verification_results
on each run):

    {
      "count": <int>,
      "weighted_count": <float>,
      "violations": [
        {"table": <str>, "row_key": <str>, "op": "insert"|"update"|"delete",
         "extra_columns": [<str>, ...], "severity": <float>}
      ],
      # Only present when the verifier couldn't run end-to-end:
      "skipped": "<reason>",
      "error":   "<details>"
    }

The verifier never raises into the eval — it always returns a block and lets
the executor decide what to attach. overall_success is never affected by
Axis 2.
"""

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# Columns that almost always change on any update and would otherwise create
# spurious diffs. Domains can extend or override via axis_2_config.
DEFAULT_IGNORED_COLUMNS = [
    "created_at",
    "updated_at",
    "modified_at",
    "created_on",
    "updated_on",
    "sys_updated_on",
    "sys_created_on",
    "sys_mod_count",
]


class Axis2Verifier:
    """Stateless per-run helper. Instantiated by the executor each run."""

    def __init__(
        self,
        mcp_clients: Dict[str, Any],
        tool_to_server_mapping: Dict[str, str],
        axis_2_config: Optional[Dict[str, Any]] = None,
    ):
        self.mcp_clients = mcp_clients
        self.tool_to_server_mapping = tool_to_server_mapping
        self.cfg = axis_2_config or {}
        # Cache PK maps keyed by (gym_name, database_id) to amortize discovery.
        self._pk_cache: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def run(
        self,
        agent_db_by_gym: Dict[str, str],
        golden_db_by_gym: Dict[str, str],
        golden_tool_calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Replay → snapshot both sides → diff → bundle the result block."""
        # 1) Replay against golden DBs.
        ok, err = await self.replay_golden(golden_db_by_gym, golden_tool_calls)
        if not ok:
            return self._skipped("golden_replay_failed", err)

        # 2) For each gym, discover PKs + snapshot both DBs and diff.
        all_violations: List[Dict[str, Any]] = []
        for gym_name, agent_db_id in agent_db_by_gym.items():
            golden_db_id = golden_db_by_gym.get(gym_name)
            if not golden_db_id:
                # No golden DB for this gym (e.g., seed failed). Skip rather
                # than half-diffing.
                return self._skipped(
                    "golden_seed_failed",
                    f"no golden database for gym '{gym_name}'",
                )

            try:
                pk_map = await self.discover_pk_map(gym_name, agent_db_id)
            except Exception as e:
                logger.exception("Axis 2: PK discovery failed for %s", gym_name)
                return self._skipped("pk_discovery_failed", str(e))

            tables = self._tables_to_diff(pk_map)

            try:
                agent_snap, golden_snap = await asyncio.gather(
                    self.snapshot(gym_name, agent_db_id, tables),
                    self.snapshot(gym_name, golden_db_id, tables),
                )
            except Exception as e:
                logger.exception("Axis 2: snapshot failed for %s", gym_name)
                return self._skipped("snapshot_failed", str(e))

            all_violations.extend(self.diff(agent_snap, golden_snap, pk_map))

        count = len(all_violations)
        weighted = sum(v["severity"] for v in all_violations)
        return {
            "count": count,
            "weighted_count": weighted,
            "violations": all_violations,
        }

    # ------------------------------------------------------------------
    # Golden replay
    # ------------------------------------------------------------------

    async def replay_golden(
        self,
        golden_db_by_gym: Dict[str, str],
        golden_tool_calls: List[Dict[str, Any]],
    ) -> Tuple[bool, Optional[str]]:
        """Sequentially execute each call against its gym's golden DB.

        We route by either an explicit ``gym_name`` on the call or the
        tool_to_server_mapping built at startup. The MCPClient is reused;
        the ``database_id`` kwarg overrides x-database-id per call without
        clobbering instance state.
        """
        for idx, call in enumerate(golden_tool_calls):
            tool_name = call.get("tool_name")
            if not tool_name:
                return False, f"golden_tool_calls[{idx}] missing tool_name"

            args = call.get("arguments") or {}
            gym_name = call.get("gym_name") or self.tool_to_server_mapping.get(tool_name)
            if not gym_name or gym_name not in self.mcp_clients:
                return (
                    False,
                    f"golden_tool_calls[{idx}] ({tool_name}): cannot resolve gym "
                    f"(call gym_name={call.get('gym_name')!r}, "
                    f"mapped={self.tool_to_server_mapping.get(tool_name)!r})",
                )

            golden_db_id = golden_db_by_gym.get(gym_name)
            if not golden_db_id:
                return (
                    False,
                    f"golden_tool_calls[{idx}] ({tool_name}): no golden DB "
                    f"for gym '{gym_name}'",
                )

            client = self.mcp_clients[gym_name]
            try:
                result = await client.call_tool(
                    tool_name, args, database_id=golden_db_id
                )
            except Exception as e:
                logger.exception("Axis 2: golden replay raised on call %d", idx)
                return False, f"call {idx} ({tool_name}) raised: {e}"

            if not result.get("success"):
                return (
                    False,
                    f"call {idx} ({tool_name}) failed: "
                    f"{result.get('error') or result}",
                )

        return True, None

    # ------------------------------------------------------------------
    # PK discovery
    # ------------------------------------------------------------------

    async def discover_pk_map(
        self, gym_name: str, database_id: str
    ) -> Dict[str, List[str]]:
        """Return ``{table: [pk_columns_in_order]}`` for the given DB.

        Tables without an explicit PK are still listed but with an empty PK
        list — those tables will be excluded from diffing (we can't form a
        stable row key).
        """
        cache_key = (gym_name, database_id)
        if cache_key in self._pk_cache:
            return self._pk_cache[cache_key]

        # 1) list tables
        rows = await self._sql_rows(
            gym_name,
            database_id,
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%';",
        )
        table_names = [r["name"] for r in rows if isinstance(r, dict) and "name" in r]

        # 2) per-table PRAGMA table_info
        pk_map: Dict[str, List[str]] = {}
        for t in table_names:
            try:
                info = await self._sql_rows(
                    gym_name, database_id, f"PRAGMA table_info({t});"
                )
            except Exception:
                logger.warning("Axis 2: PRAGMA table_info(%s) failed; skipping", t)
                pk_map[t] = []
                continue
            # `pk` column > 0 means the column is part of the PK; the value
            # is the 1-based position within a composite PK.
            pk_cols: List[Tuple[int, str]] = sorted(
                [
                    (int(row.get("pk") or 0), str(row.get("name")))
                    for row in info
                    if isinstance(row, dict)
                    and int(row.get("pk") or 0) > 0
                    and row.get("name") is not None
                ]
            )
            pk_map[t] = [name for _, name in pk_cols]

        self._pk_cache[cache_key] = pk_map
        return pk_map

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def snapshot(
        self, gym_name: str, database_id: str, tables: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """SELECT * FROM each table in parallel; return {table: [rows]}."""
        if not tables:
            return {}

        async def _one(t: str) -> Tuple[str, List[Dict[str, Any]]]:
            rows = await self._sql_rows(
                gym_name, database_id, f"SELECT * FROM {t};"
            )
            # Normalize: always a list of row-dicts.
            norm: List[Dict[str, Any]] = []
            if isinstance(rows, list):
                norm = [r for r in rows if isinstance(r, dict)]
            elif isinstance(rows, dict):
                norm = [rows]
            return t, norm

        results = await asyncio.gather(*[_one(t) for t in tables])
        return dict(results)

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(
        self,
        agent_snap: Dict[str, List[Dict[str, Any]]],
        golden_snap: Dict[str, List[Dict[str, Any]]],
        pk_map: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """Row-level set diff per table keyed by PK tuple."""
        default_sev = float(self.cfg.get("default_severity", 1.0))
        severity_overrides = self.cfg.get("severity_overrides", {}) or {}
        ignored_cfg = self.cfg.get("ignored_columns", {}) or {}
        default_ignored = set(ignored_cfg.get("_default", DEFAULT_IGNORED_COLUMNS))

        violations: List[Dict[str, Any]] = []
        # Iterate over union so a table that exists only on one side still gets
        # checked (e.g., agent created a table — shouldn't be possible, but
        # be defensive).
        for table in set(agent_snap) | set(golden_snap):
            pk_cols = pk_map.get(table) or []
            ignored = default_ignored | set(ignored_cfg.get(table, []))
            severity = float(severity_overrides.get(table, default_sev))

            if not pk_cols:
                # No PRAGMA-visible PK → fall back to whole-row identity so the
                # table is still diffed. Seed dumps are INSERT-only and many
                # schemas carry no declared PK; skipping these outright turned
                # Axis 2 into a no-op (every table excluded → always 0). A value
                # change on an existing row surfaces as one delete + one insert
                # (there is no key to pair them), which is fine for over-action.
                violations.extend(
                    self._keyless_diff(
                        table,
                        agent_snap.get(table, []),
                        golden_snap.get(table, []),
                        ignored,
                        severity,
                    )
                )
                continue

            agent_rows = {self._row_key(r, pk_cols): r for r in agent_snap.get(table, [])}
            golden_rows = {self._row_key(r, pk_cols): r for r in golden_snap.get(table, [])}

            # INSERTS — present in agent, not in golden.
            for key in agent_rows.keys() - golden_rows.keys():
                row = agent_rows[key]
                cols = [
                    c for c, v in row.items()
                    if v is not None and c not in ignored
                ]
                violations.append(
                    {
                        "table": table,
                        "row_key": self._format_row_key(key),
                        "op": "insert",
                        "extra_columns": cols,
                        "severity": severity,
                    }
                )

            # DELETES — present in golden, not in agent.
            for key in golden_rows.keys() - agent_rows.keys():
                violations.append(
                    {
                        "table": table,
                        "row_key": self._format_row_key(key),
                        "op": "delete",
                        "extra_columns": [],
                        "severity": severity,
                    }
                )

            # UPDATES — same key, differing columns.
            for key in agent_rows.keys() & golden_rows.keys():
                a, g = agent_rows[key], golden_rows[key]
                diff_cols = [
                    c for c in a
                    if c not in ignored and a.get(c) != g.get(c)
                ]
                if diff_cols:
                    violations.append(
                        {
                            "table": table,
                            "row_key": self._format_row_key(key),
                            "op": "update",
                            "extra_columns": diff_cols,
                            "severity": severity,
                        }
                    )

        return violations

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tables_to_diff(self, pk_map: Dict[str, List[str]]) -> List[str]:
        """Apply axis_2_config.tables allow-list if present; otherwise diff
        every non-system table that has a PK (others are skipped in diff())."""
        allow_list = self.cfg.get("tables")
        if allow_list:
            return [t for t in allow_list if t in pk_map]
        return list(pk_map.keys())

    @staticmethod
    def _row_key(row: Dict[str, Any], pk_cols: List[str]) -> Tuple[Any, ...]:
        return tuple(row.get(c) for c in pk_cols)

    @staticmethod
    def _format_row_key(key: Tuple[Any, ...]) -> str:
        # Single PK → just the value as a string. Composite → "a|b|c".
        if len(key) == 1:
            return str(key[0])
        return "|".join("" if k is None else str(k) for k in key)

    # ---- keyless (no-PK) fallback -------------------------------------

    @staticmethod
    def _value_sig(v: Any) -> Any:
        """Hashable, comparable form of a cell value."""
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        return json.dumps(v, sort_keys=True, default=str)

    @classmethod
    def _row_signature(
        cls, row: Dict[str, Any], ignored: set
    ) -> Tuple[Tuple[str, Any], ...]:
        """Order-independent signature of a row over its non-ignored columns."""
        return tuple(
            sorted((c, cls._value_sig(v)) for c, v in row.items() if c not in ignored)
        )

    @staticmethod
    def _sig_key(sig: Tuple[Tuple[str, Any], ...]) -> str:
        return "row:" + hashlib.md5(repr(sig).encode()).hexdigest()[:10]

    def _keyless_diff(
        self,
        table: str,
        agent_rows: List[Dict[str, Any]],
        golden_rows: List[Dict[str, Any]],
        ignored: set,
        severity: float,
    ) -> List[Dict[str, Any]]:
        """Whole-row multiset diff for tables with no usable primary key.

        Rows seeded identically into both DBs cancel out; only the divergence
        between the agent's writes and the golden replay's writes remains.
        Counter difference handles duplicate rows correctly.
        """
        from collections import Counter

        agent_ct = Counter(self._row_signature(r, ignored) for r in agent_rows)
        golden_ct = Counter(self._row_signature(r, ignored) for r in golden_rows)

        out: List[Dict[str, Any]] = []
        for sig, n in (agent_ct - golden_ct).items():  # extra in agent → insert
            cols = [c for c, v in sig if v is not None]
            for _ in range(n):
                out.append({
                    "table": table,
                    "row_key": self._sig_key(sig),
                    "op": "insert",
                    "extra_columns": cols,
                    "severity": severity,
                })
        for sig, n in (golden_ct - agent_ct).items():  # missing in agent → delete
            for _ in range(n):
                out.append({
                    "table": table,
                    "row_key": self._sig_key(sig),
                    "op": "delete",
                    "extra_columns": [],
                    "severity": severity,
                })
        return out

    @staticmethod
    def _skipped(reason: str, error: Optional[str]) -> Dict[str, Any]:
        block: Dict[str, Any] = {
            "count": 0,
            "weighted_count": 0.0,
            "violations": [],
            "skipped": reason,
        }
        if error:
            block["error"] = error
        return block

    async def _sql_rows(
        self, gym_name: str, database_id: str, query: str
    ) -> List[Dict[str, Any]]:
        """POST a SQL query to ``/api/sql-runner`` and return its rows.

        Matches the contract of VerifierEngine._execute_sql_query but always
        returns a list — single-row results are wrapped, value-only results
        raise (we only call this with multi-row SELECTs and PRAGMAs).
        """
        client = self.mcp_clients[gym_name]
        api_url = f"{client.base_url.rstrip('/')}/api/sql-runner"

        payload = {"query": query, "database_id": database_id}
        headers = {
            "Content-Type": "application/json",
            "x-database-id": database_id,
        }
        headers.update(client._get_auth_headers())
        if client.context:
            for key, value in client.context.items():
                if not key.lower().startswith("x-"):
                    header_key = f"x-{key.lower().replace('_', '-')}"
                else:
                    header_key = key
                headers[header_key] = str(value)

        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            api_result = response.json()

        # The sql-runner returns rows under either `data` or `rows`, mirroring
        # what verifier.py:_extract_value_from_sql_result handles.
        for key in ("data", "rows"):
            if isinstance(api_result.get(key), list):
                return [r for r in api_result[key] if isinstance(r, dict)]

        # Some responses wrap a nested object — accept that too.
        nested = api_result.get("result")
        if isinstance(nested, dict):
            for key in ("data", "rows"):
                if isinstance(nested.get(key), list):
                    return [r for r in nested[key] if isinstance(r, dict)]

        # Empty result is a valid outcome (empty table).
        if api_result == {} or api_result.get("data") in (None, []):
            return []

        raise RuntimeError(
            f"sql-runner returned unexpected shape for query "
            f"{query!r}: {json.dumps(api_result)[:300]}"
        )
