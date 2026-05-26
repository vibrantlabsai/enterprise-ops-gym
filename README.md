<div align="center">

<h1>EnterpriseOps-Gym</h1>

<p>
  <a href="https://huggingface.co/datasets/vibrantlabsai/enterprise-ops-gym-plus"><img src="https://img.shields.io/badge/🤗_Dataset-yellow" /></a>
  <img src="https://img.shields.io/badge/License-CC%20BY--NC%204.0-lightgrey" />
</p>

<p><i>A containerized, resettable enterprise simulation for evaluating LLM agents on stateful, multi-step planning and tool use — single-turn, multi-turn (with a user simulator), and unintended-change detection.</i></p>

<p><sub>Forked from the EnterpriseOps-Gym benchmark.</sub></p>

</div>

---

## 📖 Overview

EnterpriseOps-Gym runs agent tasks against **live MCP servers** backed by per-task SQLite databases that are **seeded fresh and reset between runs**. Tasks are graded by **SQL verifiers that check the final environment state** — not the action sequence — so an agent is free to reach the goal however it likes.

This fork adds two evaluation surfaces on top of the standard single-turn ReAct loop:

- **Multi-turn (ReAct + user simulator).** Instead of receiving the whole task up front, the agent converses with an LLM that plays the **user**, who discloses details progressively as the agent asks.
- **Axis 2 — unintended-change detection.** A row-level **database state diff** against a golden replay that flags writes the agent made *beyond* what the task required.

Three things are graded independently:

| Surface | Question it answers | How |
|---|---|---|
| Verifiers (Axis 1) | Did the required things happen? | SQL predicates over the final DB |
| Multi-turn | Can the agent gather the task through dialogue? | LLM user simulator + the same verifiers |
| Axis 2 | Did anything **extra** happen? | DB row diff vs. a golden replay |

---

## 📋 Table of Contents

- [⚙️ Installation](#️-installation)
- [🔧 Prerequisites](#-prerequisites)
- [🚀 Running the Benchmark](#-running-the-benchmark)
- [💬 Multi-turn (ReAct + user simulator)](#-multi-turn-react--user-simulator)
- [🧬 Axis 2 — unintended-change detection](#-axis-2--unintended-change-detection)
- [📊 Scoring](#-scoring)
- [🙏 Acknowledgements](#-acknowledgements)

---

## ⚙️ Installation

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/vibrantlabsai/enterprise-ops-gym.git
cd enterprise-ops-gym

# Install with only the provider(s) you need
uv sync --extra anthropic    # Claude / AWS Bedrock
uv sync --extra openai       # OpenAI / Azure OpenAI
uv sync --extra google       # Gemini / Vertex AI
uv sync --extra deepseek     # DeepSeek
uv sync --extra all          # Everything
```

Copy and configure the example configs:

```bash
cp -r conf.example/ conf/
# Edit conf/llm/my-model.json with your API key and model details
```

---

## 🔧 Prerequisites

### 1. Seed Databases

Each task runs against a pre-populated database seeded from a SQL snapshot. These snapshots are bundled in `gym_dbs.zip` at the root of the repository — one SQL file per unique database, organized by domain:

```
Domain Wise DBs and Task-DB Mappings/
  calendar/dbs/   # Calendar domain database snapshots
  csm/dbs/        # Customer Service Management snapshots
  drive/dbs/      # Drive domain snapshots
  email/dbs/      # Email domain snapshots
  hr/dbs/         # HR domain snapshots
  hybrid/dbs/     # Multi-domain (hybrid) snapshots
  itsm/dbs/       # IT Service Management snapshots
  teams/dbs/      # Teams domain snapshots
```

Unzip it before running the benchmark:

```bash
unzip gym_dbs.zip
```

### 2. Gym Servers

Each domain requires a running MCP server. Pull and start the Docker image for each domain:

```bash
docker pull shivakrishnareddyma225/enterpriseops-gym-mcp-<domain>:latest
docker run -d -p <host_port>:<container_port> shivakrishnareddyma225/enterpriseops-gym-mcp-<domain>:latest
```

Default ports:

| Domain | MCP Server | Port |
|--------|-----------|------|
| `teams` | `gym-teams-mcp` | 8002 |
| `csm` | `gym-csm-server` | 8001 |
| `email` | `gym-email-mcp` | 8004 |
| `itsm` | `gym-itsm-mcp` | 8006 |
| `calendar` | `gym-calendar` | 8003 |
| `hr` | `gym-hr-internal` | 8008 |
| `drive` | `gym-google-drive-mcp` | 8009 |

Update `conf/ray/domain_conf.json` if you use non-default ports. For `calendar` use 8003 as the container_port.

### 3. LLM Config

LLM configs live in `conf/llm/<name>.json`. Use an array for load-balanced pools.

| Field | Required | Description |
|-------|----------|-------------|
| `llm_provider` | ✅ | `anthropic`, `aws_bedrock`, `openai`, `azureopenai`, `googlevertexai`, `google`, `vllm`, `openrouter`, `deepseek`, `qwq` |
| `llm_model` | ✅ | Model identifier |
| `llm_api_key` | ✅ | API key (may be empty for providers that read credentials from the environment, e.g. AWS Bedrock) |
| `llm_api_endpoint` | — | Required for Azure OpenAI / vLLM |
| `llm_api_version` | — | Required for Azure OpenAI |
| `llm_region` | — | Region for `aws_bedrock` / `googlevertexai` |
| `temperature` | — | Default `0.0`; set `null` to omit it for models that reject a custom temperature |
| `max_tokens` | — | Default `4096` |

```json
{
    "llm_provider": "azureopenai",
    "llm_model": "gpt-4.1",
    "llm_api_key": "<your-api-key>",
    "llm_api_endpoint": "https://<your-resource>.openai.azure.com",
    "llm_api_version": "2025-04-01-preview",
    "temperature": 0.1,
    "max_tokens": 16384
}
```

---

## 🚀 Running the Benchmark

A run reads task configs from either a HuggingFace dataset (`--hf_dataset` with `--mode` = config name and `--domain` = split) or a local folder (`--configs_folder`). Each task becomes one `BenchmarkConfig`.

### Option A — Ray *(recommended for batch runs)*

Ray orchestrates parallel runs across models and domains.

**1. Create an experiment config** (`conf/ray/experiment.json`):

```json
{
    "llms": ["gpt-4.1-mini", "gemini_2p5"],
    "domains": ["teams", "csm", "email"],
    "modes": ["oracle"],
    "orchestrator": "react",
    "num_runs": 1,
    "num_llm_instances": 1,
    "path_templates": {
        "log_dir": "logs/{orchestrator}/{llm}/{domain}/{mode}",
        "output_folder": "results/{orchestrator}/{llm}/{domain}/{mode}",
        "llm_config": "conf/llm/{llm}.json"
    }
}
```

Per-model task concurrency is set in `conf/ray/llm_concurrency.json` (defaults to 5).

**2. Run:**

```bash
python ray_experiment_queue.py --experiment_config conf/ray/experiment.json
```

### Option B — Direct (single-turn ReAct)

Run a single domain/mode without Ray:

```bash
python evaluate.py \
    --hf_dataset vibrantlabsai/enterprise-ops-gym-plus \
    --domain itsm --mode oracle \
    --llm_config conf/llm/my-model.json \
    --output_folder results/react/my-model/itsm/oracle \
    --orchestrator react \
    --concurrency 4 --num_runs 1
```

**Orchestrators:**

| Value | Description |
|-------|-------------|
| `react` | Standard ReAct loop |
| `planner_react` | Planner generates a plan; executor follows it |
| `decomposing` | Decomposes the task into sub-goals before executing |
| `multiturn_react` | ReAct loop with an LLM user simulator on the other end (see below) |

For `planner_react` / `decomposing`, add `--planner_llm_config conf/llm/<planner>.json`.

---

## 💬 Multi-turn (ReAct + user simulator)

`multiturn_react` evaluates the agent against an LLM playing the **user** role, instead of handing it the full task prompt up front. The agent has to *extract* the task through conversation — which surfaces real-world failure modes (missed prerequisites, wrong argument literals under noise, not asking for required fields). Tool calls still execute against the MCP servers exactly as in single-turn ReAct; any plain-text the agent produces is delivered to the user simulator.

**How it works** (`orchestrators/multiturn_react.py`, `user_simulator/simulator.py`):

- Each task row carries a **`scenario`** object:

  ```json
  {
    "domain": "itsm",
    "reason_for_call": "why the user is reaching out (revealed first, paraphrased)",
    "known_info": "facts the user holds and shares only when asked",
    "task_instructions": "behavioral direction: pacing, what to withhold, when to stop"
  }
  ```

- The user simulator **discloses information progressively** — one piece at a time, only when the agent asks — and **never fabricates** anything outside the scenario.
- Its system prompt is an editable plain-text file, `user_simulator/user_sim_system_prompt.txt`, loaded at build time with the scenario appended (`user_simulator/prompts.py::build_user_simulator_prompt`). Tune behavior by editing the file — no code change.
- The conversation ends when the user emits a control token or the turn budget is hit:
  - **`###STOP###`** — the user considers the task complete.
  - **`###OUT-OF-SCOPE###`** — the scenario does not contain the information needed to continue (e.g. the agent demands an identifier the user was never given).

**Required flags:**

- `--orchestrator multiturn_react`
- `--user_simulator_llm_config conf/llm/<user-sim>.json` — the LLM that plays the user (can match the agent or be a smaller/cheaper model)
- `--max_user_turns <N>` — cap on agent↔user round-trips per task (default `20`)

The task row **must have a `scenario` column**. The `vibrantlabsai/enterprise-ops-gym-plus` config **`qwen-3.6-27B-multiturn`** ships scenarios whose `task_instructions` were derived empirically from agent rollouts:

```bash
python evaluate.py \
    --hf_dataset vibrantlabsai/enterprise-ops-gym-plus \
    --domain itsm --mode qwen-3.6-27B-multiturn \
    --llm_config conf/llm/my-model.json \
    --user_simulator_llm_config conf/llm/user-sim.json \
    --output_folder results/multiturn_react/my-model/itsm \
    --orchestrator multiturn_react \
    --max_user_turns 20 \
    --concurrency 5 --num_runs 1
```

The user-sim config uses the same schema as any other LLM config. A higher temperature (~0.7) gives more natural variation; `max_tokens` of 512–1024 is plenty since replies are short:

```json
{
    "llm_provider": "aws_bedrock",
    "llm_model": "us.anthropic.claude-sonnet-4-6",
    "llm_api_key": "",
    "llm_region": "us-east-1",
    "temperature": 0.7,
    "max_tokens": 1024
}
```

Scoring is identical to the single-turn orchestrators. Expect **lower** scores than `react` on the same tasks — the agent now has to earn the task details through dialogue.

---

## 🧬 Axis 2 — unintended-change detection

The standard `database_state` verifiers (**Axis 1**) answer *"did the required things happen?"*. **Axis 2** answers the complementary question — *"did anything **extra** happen?"* — by diffing the agent's final database against a **golden replay** of the canonical solution.

**Mechanism** (`benchmark/axis2_verifier.py`, `benchmark/executor.py::_compute_axis_2`), per gym, per run:

1. The executor seeds a **second "golden" database** from the same seed file as the agent's DB.
2. After the agent runs, the task's `golden_tool_calls` are **replayed** against the golden DB (re-using the MCP client pointed at the golden `database_id`).
3. Both DBs are snapshotted (`SELECT * FROM <table>`), primary keys are discovered server-side (`sqlite_master` + `PRAGMA table_info`).
4. Rows are diffed **by primary key** → `insert` (extra row in agent DB), `delete` (missing row), `update` (changed columns), with timestamp-like columns ignored by default.

**Enable it** on a task config:

```json
{
    "compute_axis_2": true,
    "golden_tool_calls": [
        {"tool_name": "update_incident",
         "arguments": {"incident_id": "INC_004", "status": "in_progress"},
         "gym_name": "gym-itsm-mcp"}
    ],
    "axis_2_config": {
        "tables": ["incident", "notification"],
        "ignored_columns": {"_default": ["created_at", "updated_at"], "incident": ["sys_mod_count"]},
        "default_severity": 1.0,
        "severity_overrides": {"notification": 0.5}
    }
}
```

- `golden_tool_calls` — required when `compute_axis_2` is true (the write sequence to replay).
- `axis_2_config` — optional knobs: a `tables` allow-list, per-table `ignored_columns` (extends the timestamp defaults), and severity weights.

**Output.** A top-level `axis_2_unintended_changes` block is attached to each run, alongside `verification_results`:

```json
{
  "count": 1,
  "weighted_count": 1.0,
  "violations": [
    {"table": "incident", "row_key": "INC_009", "op": "update",
     "extra_columns": ["assigned_to"], "severity": 1.0}
  ]
}
```

If the verifier can't run end-to-end it emits a `"skipped": "<reason>"` (and `"error"`) block instead. **Axis 2 never affects `overall_success`** — it is a separate signal for measuring over-action.

> The per-task `golden_tool_calls` can be authored by hand or extracted from passing solver runs with the repo's golden-extraction tooling.

---

## 📊 Scoring

```bash
# Single run
python compute_score.py --results_folder results/react/my-model/itsm/oracle

# All modes at once
python compute_score.py --results_folder results/react/my-model/itsm
```

Output:

```
+----------------+---------------+-----------------+----------------------+-----------------------+
| Mode           | Total Files   | Files w/ Errors | Avg Success Rate (%) | Avg Verifier Pass (%) |
+================+===============+=================+======================+=======================+
| oracle         | 100           | 0               | 72.00                | 68.50                 |
+----------------+---------------+-----------------+----------------------+-----------------------+
```

- **Avg Success Rate** — tasks where *all* verifiers passed
- **Avg Verifier Pass** — average per-verifier pass rate
- **Files w/ Errors** — agent errors; excluded from averages

`compute_score.py` reads `verification_results` from each task file, so it works the same across all orchestrators. The `axis_2_unintended_changes` block is reported separately and does not enter the success rate.

---

## 🙏 Acknowledgements

Built on the **EnterpriseOps-Gym** benchmark. Released under **CC BY-NC 4.0**.
