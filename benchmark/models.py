from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class VerifierType(str, Enum):
    DATABASE_STATE = "database_state"
    RESPONSE_CHECKER = "response_check"
    TOOL_EXECUTION = "tool_execution"


@dataclass
class MCPToolCall:
    """Represents an MCP tool call"""

    tool_name: str
    arguments: Dict[str, Any]


@dataclass
class MCPToolResponse:
    """Represents an MCP tool response"""

    success: bool
    result: Any = None
    error: Optional[str] = None


@dataclass
class VerifierConfig:
    """Configuration for a verifier"""

    verifier_type: str
    validation_config: Dict[str, Any]
    name: Optional[str] = None
    description: Optional[str] = None
    gym_name: Optional[str] = None  # Which gym's database to query (for multi-gym)


@dataclass
class GymServerConfig:
    """Configuration for a single gym server"""

    mcp_server_name: str
    mcp_server_url: str
    mcp_endpoint: str = "/mcp"
    seed_database_file: str = ""
    auth_config: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    database_id: str = ""  # Populated at runtime (not from config)


@dataclass
class BenchmarkConfig:
    """Complete benchmark configuration from config.json

    Supports both single-gym (legacy) and multi-gym configurations:
    - Single-gym: Uses mcp_server_url, database_id
    - Multi-gym: Uses gym_servers_config array
    """

    system_prompt: str
    user_prompt: str
    verifiers: List[Dict[str, Any]]
    number_of_runs: int

    # Multi-gym configuration (preferred)
    gym_servers_config: Optional[List[Dict[str, Any]]] = None

    # Legacy single-gym configuration (backward compatibility)
    mcp_server_url: Optional[str] = None
    mcp_endpoint: Optional[str] = "/mcp"
    database_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    auth_config: Optional[Dict[str, Any]] = None

    # Other options
    selected_tools: Optional[List[str]] = None
    restricted_tools: Optional[List[str]] = None
    temperature: float = 0.0
    max_tokens: int = 4096
    reset_database_between_runs: bool = True

    # Multi-turn user-scenario instructions (multiturn_react orchestrator).
    # Expected keys: domain, reason_for_call, known_info, task_instructions.
    scenario: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Axis 2: unintended DB writes detection (opt-in).
    # See benchmark/axis2_verifier.py.
    #   compute_axis_2:    flip to true to run the row-level diff each run.
    #   golden_tool_calls: per-task list of {tool_name, arguments[, gym_name]}
    #                     describing the canonical correct write sequence.
    #                     Required when compute_axis_2 is true; otherwise the
    #                     verifier emits skipped="no_golden".
    #   axis_2_config:    optional knobs — tables allow-list, ignored_columns,
    #                     severity_overrides, default_severity. See module doc.
    # NOTE: If this field ever appears as a JSON-serialized string in a HF
    # dataset row, add "golden_tool_calls" to json_string_fields in evaluate.py.
    # ------------------------------------------------------------------
    compute_axis_2: bool = False
    golden_tool_calls: Optional[List[Dict[str, Any]]] = None
    axis_2_config: Optional[Dict[str, Any]] = None


@dataclass
class LLMConfig:
    llm_provider: str  # "anthropic", "openai", "google", "azureopenai"
    llm_model: str
    llm_api_key: str
    llm_api_endpoint: Optional[str] = ""
    llm_api_version: Optional[str] = ""
    llm_region: Optional[str] = None
    temperature: Optional[float] = 0.0
    max_tokens: int = 4096
    top_p: Optional[float] = None
    effort: Optional[str] = None
    reasoning: Optional[dict] = None
