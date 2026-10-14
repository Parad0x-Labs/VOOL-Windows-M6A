"""Early turn faults close their original attempt and execution without publishing an answer."""
from __future__ import annotations

from unittest import mock

from tests.test_a9_routing_context_retry_pass002 import _RunOnceHarness


class EarlyExecutionFailureTests(_RunOnceHarness):
    def _assert_failure_at(self, target: str) -> None:
        from core.finalization import get_finalization_by_request_id
        from core.semantic.semantic_admissions import current_request_id
        from core.turn_contract import TURN_REQUEST_KEY

        agent = self._agent()
        error = RuntimeError("synthetic early execution failure")
        context = {"surface": "test"}
        previous_request = current_request_id()
        with mock.patch.object(type(agent), "_run_once_inner", return_value={"success": False}):
            with mock.patch(target, side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    agent.run_once(
                        "synthetic failure request", session_id_override="sess-a9p2", source_context=context,
                    )
        self.assertIs(raised.exception, error, "cleanup must preserve the original exception")
        identity = context["_execution_identity"]
        request = context[TURN_REQUEST_KEY]
        attempt = self._last_attempt()
        execution = self._exec_row(str(attempt["root_attempt_id"]))
        self.assertEqual(attempt["attempt_id"], identity["attempt_id"])
        self.assertEqual(execution["execution_id"], identity["execution_id"])
        self.assertEqual(execution["request_id"], request.request_id)
        self.assertEqual(attempt["lifecycle_state"], "FAILED_PROVIDER")
        self.assertEqual(execution["state"], "FAILED")
        self.assertTrue(attempt["completed_at"])
        self.assertIsNone(get_finalization_by_request_id(request.request_id, principal="owner_local"))
        self.assertEqual(current_request_id(), previous_request)

    def test_failure_before_observation_closes_both_ledgers(self) -> None:
        self._assert_failure_at("core.context_retrieval.reset_retrieval_telemetry")

    def test_failure_starting_observation_closes_both_ledgers(self) -> None:
        self._assert_failure_at("core.semantic.turn_observation.observe_turn")

    def test_inner_failure_preserves_exception_and_request_identity(self) -> None:
        self._assert_failure_at("core.agent_runtime.agent.VoolAgent._run_once_inner")

    def test_failure_constructing_turn_state_never_becomes_success(self) -> None:
        self._assert_failure_at("core.turn_contract.TurnState.from_intake")
