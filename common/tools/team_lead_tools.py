"""Team Lead tool barrel.

Import this module to register all tools needed by the Team Lead Agent.
Includes dev-agent tools plus Registry discovery and launch tools.
"""

import common.tools.jira_tools        # noqa: F401
import common.tools.scm_tools         # noqa: F401
import common.tools.design_tools      # noqa: F401
import common.tools.progress_tools    # noqa: F401
import common.tools.registry_tools    # noqa: F401 — registers RegistryQueryTool, check_agent_status, list_available_agents
import common.tools.skill_tool        # noqa: F401 — registers load_skill, list_skills (catalog-aware)
import common.tools.control_tools     # noqa: F401 — registers dispatch_agent_task, wait_for_agent_task, ack_agent_task, etc.
import common.tools.validation_tools  # noqa: F401 — registers run_validation_command, collect_task_evidence, etc.
