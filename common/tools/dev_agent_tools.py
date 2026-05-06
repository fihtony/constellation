"""Dev agent tool barrel.

Import this module to register all tools needed by development agents
(Web Agent, Android Agent, etc.).  Importing triggers side-effects
(self-registration) but has no other observable effect.
"""

import common.tools.jira_tools      # noqa: F401 — registers JiraGetTicketTool, JiraAddCommentTool
import common.tools.scm_tools       # noqa: F401 — registers ScmCreateBranchTool, ScmPushFilesTool, ScmCreatePRTool, read-only tools
import common.tools.design_tools    # noqa: F401 — registers FigmaFetchScreenTool, StitchFetchScreenTool
import common.tools.progress_tools  # noqa: F401 — registers ReportProgressTool
import common.tools.control_tools   # noqa: F401 — registers dispatch_agent_task, wait_for_agent_task, ack_agent_task, etc.
