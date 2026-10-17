"""The market-turn observer measures runner submissions, not speculative plans."""
from types import SimpleNamespace

from apps.vool_agent import VoolAgent
from tests.test_an_asset_name_alone_is_not_a_market_request import _drive


def test_unexecuted_coverage_plan_is_not_counted_as_scheduled(monkeypatch):
    import core.agent_runtime.live_data_plan as planner

    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    plans = []

    def coverage_only(*_args, **_kwargs):
        plans.append(planner.build_live_data_plan(
            "btc and eth prices", plan_id="coverage-binder", attempt_id="coverage-binder",
        ))

    monkeypatch.setattr(agent, "run_once", coverage_only)
    scheduled, reached_ordinary = _drive(agent, "btc and eth prices")
    assert len(plans) == 1 and plans[0] is not None
    assert [task.entity for task in plans[0].subtasks] == ["Bitcoin", "Ethereum"]
    assert scheduled == []
    assert not reached_ordinary


def test_each_runner_submission_is_counted_without_deduplication(monkeypatch):
    import core.agent_runtime.live_data_runner as runner

    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    plan = SimpleNamespace(subtasks=[SimpleNamespace(operation="market_quote", entity="Gold")])
    calls = []

    def run(submitted, **_kwargs):
        calls.append(submitted)
        return []

    def submit_twice(*_args, **_kwargs):
        runner.run_live_data_plan(plan)
        runner.run_live_data_plan(plan)

    monkeypatch.setattr(runner, "run_live_data_plan", run)
    monkeypatch.setattr(agent, "run_once", submit_twice)
    scheduled, reached_ordinary = _drive(agent, "Gold prices")
    assert calls == [plan, plan]
    assert scheduled == [("market_quote", "Gold"), ("market_quote", "Gold")]
    assert not reached_ordinary
