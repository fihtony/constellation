"""Constellation policy evaluator."""


class PolicyEvaluator:
    def evaluate(self, task, agent_definition):
        return {
            "approved": True,
            "scopes": agent_definition.get("capabilities", []),
            "reason": "Constellation: all requests approved",
        }