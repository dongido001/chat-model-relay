from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api import openai_routes
from src.api.conversation_store import ConversationStore
from src.api.openai_schemas import ChatCompletionRequest, ChatMessage
from src.config import Config


class _RoutingClient:
    def __init__(self, thread_id: str = "") -> None:
        self.thread_id = thread_id
        self.new_chat_calls = 0
        self.navigate_calls: list[str] = []
        self.fail_navigation = False

    def _extract_thread_id(self) -> str:
        return self.thread_id

    async def new_chat(self) -> None:
        self.new_chat_calls += 1
        self.thread_id = ""

    async def navigate_to_thread(self, thread_id: str) -> None:
        self.navigate_calls.append(thread_id)
        if self.fail_navigation:
            raise RuntimeError("stale")
        self.thread_id = thread_id


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.tempdir.name) / "conversations.sqlite3")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_route_round_trip_and_revision(self) -> None:
        route = self.store.save_route(
            project_key="global", app_key="app", conversation_key="conv",
            thread_id="thread-1", transcript=[{"role": "user", "content": "hello"}],
            message_hashes=["h1"], contract_hash="contract",
        )
        self.assertEqual(route.revision, 1)
        updated = self.store.save_route(
            project_key="global", app_key="app", conversation_key="conv",
            thread_id="thread-1", transcript=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ], message_hashes=["h1", "h2"], contract_hash="contract",
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(len(updated.transcript), 2)

    def test_response_snapshot_round_trip(self) -> None:
        route = self.store.save_route(
            project_key="global", app_key="default", conversation_key="conv",
            thread_id="thread-1", transcript=[{"role": "user", "content": "hello"}],
            message_hashes=["h1"], contract_hash="",
        )
        self.store.save_response("resp_1", route)
        response = self.store.get_response("resp_1")
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.conversation_key, "conv")
        self.assertEqual(response.revision, route.revision)

    def test_routes_are_partitioned_by_project_and_app(self) -> None:
        for project, app, thread in (("p1", "a", "t1"), ("p2", "a", "t2"), ("p1", "b", "t3")):
            self.store.save_route(
                project_key=project, app_key=app, conversation_key="same",
                thread_id=thread, transcript=[], message_hashes=[], contract_hash="",
            )
        self.assertEqual(self.store.get_route("p1", "a", "same").thread_id, "t1")
        self.assertEqual(self.store.get_route("p2", "a", "same").thread_id, "t2")
        self.assertEqual(self.store.get_route("p1", "b", "same").thread_id, "t3")

    def test_prune_bounds_route_count(self) -> None:
        for index in range(3):
            self.store.save_route(
                project_key="global", app_key="app", conversation_key=f"c{index}",
                thread_id=f"t{index}", transcript=[], message_hashes=[], contract_hash="",
            )
            time.sleep(0.01)
        self.store.prune(retention_seconds=3600, max_routes=2)
        self.assertIsNone(self.store.get_route("global", "app", "c0"))
        self.assertIsNotNone(self.store.get_route("global", "app", "c2"))


class ConversationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "routes.sqlite3"
        self.db_patch = patch.object(openai_routes.Config, "API_CONVERSATION_DB", self.db_path)
        self.project_patch = patch.object(Config, "CHATGPT_PROJECT_URL", "")
        self.db_patch.start()
        self.project_patch.start()
        openai_routes._conversation_store = None
        openai_routes._conversation_store_path = ""

    async def asyncTearDown(self) -> None:
        self.db_patch.stop()
        self.project_patch.stop()
        openai_routes._conversation_store = None
        openai_routes._conversation_store_path = ""
        self.tempdir.cleanup()

    async def test_verified_full_history_forwards_only_delta(self) -> None:
        client = _RoutingClient()
        first_request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="one")])
        initial = await openai_routes._prepare_conversation_routing(
            client, first_request, app_key="app", conversation_key="conv"
        )
        transcript = [*initial.transcript_input, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        full = ChatCompletionRequest(messages=[
            ChatMessage(role="user", content="one"),
            ChatMessage(role="assistant", content="answer"),
            ChatMessage(role="user", content="two"),
        ])
        routing = await openai_routes._prepare_conversation_routing(
            client, full, app_key="app", conversation_key="conv"
        )
        self.assertEqual(routing.action, "verified-prefix-delta")
        self.assertEqual([message.content for message in routing.messages_for_browser], ["two"])
        self.assertEqual(client.navigate_calls, ["thread-1"])

    async def test_diverged_history_starts_fresh_thread(self) -> None:
        client = _RoutingClient("thread-1")
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="old")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="conv"
        )
        transcript = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        diverged = ChatCompletionRequest(messages=[
            ChatMessage(role="user", content="different"),
            ChatMessage(role="assistant", content="history"),
            ChatMessage(role="user", content="new"),
        ])
        routing = await openai_routes._prepare_conversation_routing(
            client, diverged, app_key="app", conversation_key="conv"
        )
        self.assertEqual(routing.action, "new-chat-history-diverged")
        self.assertGreaterEqual(client.new_chat_calls, 1)

    async def test_project_thread_rotates_at_size_limit_with_bounded_context(self) -> None:
        client = _RoutingClient("thread-old")
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="new task")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="project:cutline"
        )
        new_chat_before = client.new_chat_calls
        prior = [
            {"role": "user", "content": "earlier request"},
            {"role": "assistant", "content": "useful project context"},
        ]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-old",
            transcript=prior, message_hashes=[openai_routes._message_hash(item) for item in prior],
            contract_hash=initial.contract_hash,
        )
        with (
            patch.object(openai_routes.Config, "API_PROJECT_THREAD_MAX_CHARS", 1),
            patch.object(openai_routes.Config, "API_PROJECT_THREAD_CONTEXT_CHARS", 1000),
        ):
            routing = await openai_routes._prepare_conversation_routing(
                client, request, app_key="app", conversation_key="project:cutline"
            )
        self.assertEqual(routing.action, "project-thread-size-rotation")
        self.assertIsNone(routing.previous_route)
        self.assertEqual(client.new_chat_calls, new_chat_before + 1)
        contents = [message.content for message in routing.messages_for_browser]
        self.assertIn("useful project context", contents)
        self.assertEqual(contents[-1], "new task")

    async def test_project_thread_is_shared_across_client_apps(self) -> None:
        project_key = "project:cutline"
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="first task")])
        first_client = _RoutingClient("thread-project")
        initial = await openai_routes._prepare_conversation_routing(
            first_client, request, app_key="codex", conversation_key=project_key
        )
        transcript = [
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "first answer"},
        ]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-project",
            transcript=transcript, message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )

        second_client = _RoutingClient("thread-project")
        follow_up = ChatCompletionRequest(messages=[ChatMessage(role="user", content="second task")])
        routing = await openai_routes._prepare_conversation_routing(
            second_client, follow_up, app_key="githubcopilotchat", conversation_key=project_key
        )
        self.assertIsNotNone(routing.previous_route)
        self.assertEqual(routing.previous_route.thread_id, "thread-project")
        self.assertEqual(routing.messages_for_browser[0].content, "second task")
        self.assertEqual(second_client.new_chat_calls, 0)

    async def test_derived_copilot_replay_reuses_thread(self) -> None:
        client = _RoutingClient("thread-1")
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="old")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="derived:abc123"
        )
        transcript = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        new_chat_before = client.new_chat_calls
        replay = ChatCompletionRequest(messages=[
            ChatMessage(role="system", content="huge contract"),
            ChatMessage(role="user", content="old dump"),
            ChatMessage(role="assistant", content="tool json"),
            ChatMessage(role="user", content="hello"),
        ])
        routing = await openai_routes._prepare_conversation_routing(
            client, replay, app_key="app", conversation_key="derived:abc123"
        )
        self.assertEqual(routing.action, "derived-replay-delta")
        self.assertEqual([message.content for message in routing.messages_for_browser], ["hello"])
        self.assertEqual(client.new_chat_calls, new_chat_before)
        self.assertEqual(client.navigate_calls[-1], "thread-1")

    async def test_contract_change_reuses_explicit_conversation_thread(self) -> None:
        """Changing Codex tool metadata must not reset its browser conversation."""
        client = _RoutingClient("thread-1")
        initial_request = ChatCompletionRequest(
            messages=[ChatMessage(role="system", content="old contract"), ChatMessage(role="user", content="one")]
        )
        initial = await openai_routes._prepare_conversation_routing(
            client, initial_request, app_key="app", conversation_key="codex-session"
        )
        transcript = [*initial.transcript_input, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        new_chat_before = client.new_chat_calls
        changed = ChatCompletionRequest(messages=[
            ChatMessage(role="system", content="new contract"),
            ChatMessage(role="user", content="two"),
        ])

        routing = await openai_routes._prepare_conversation_routing(
            client, changed, app_key="app", conversation_key="codex-session"
        )

        self.assertEqual(routing.action, "contract-change-delta")
        self.assertEqual([message.content for message in routing.messages_for_browser], ["two"])
        self.assertEqual(client.new_chat_calls, new_chat_before)
        self.assertEqual(client.navigate_calls[-1], "thread-1")

    async def test_derived_replay_retains_question_image_and_editor_context(self) -> None:
        client = _RoutingClient("thread-1")
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="old")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="derived:abc123"
        )
        transcript = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        new_chat_before = client.new_chat_calls
        replay = ChatCompletionRequest(messages=[
            ChatMessage(role="system", content="huge contract"),
            ChatMessage(role="user", content="old dump"),
            ChatMessage(role="assistant", content="tool json"),
            ChatMessage(role="user", content="This queued shot has no indication after refresh"),
            ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,c2NyZWVu"}}]),
            ChatMessage(role="user", content='<attachment name="Browser Pages">Cutline Studio</attachment>'),
        ])
        routing = await openai_routes._prepare_conversation_routing(
            client, replay, app_key="app", conversation_key="derived:abc123"
        )
        self.assertEqual(routing.action, "derived-replay-delta")
        self.assertEqual(routing.messages_for_browser, replay.messages[-3:])
        from src.api.prompt_compaction import _new_attachments_from_latest_user, _latest_turn_messages
        pruned = _latest_turn_messages(routing.messages_for_browser, include_system=False)
        self.assertEqual(pruned, replay.messages[-3:])
        urls, _ = _new_attachments_from_latest_user(pruned)
        self.assertEqual(urls, ["data:image/png;base64,c2NyZWVu"])
        self.assertEqual(routing.transcript_input[-3]["content"], "This queued shot has no indication after refresh")
        self.assertEqual(client.new_chat_calls, new_chat_before)
        self.assertEqual(client.navigate_calls[-1], "thread-1")

    async def test_derived_new_copilot_chat_starts_fresh_thread(self) -> None:
        client = _RoutingClient("thread-1")
        first = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        initial = await openai_routes._prepare_conversation_routing(
            client, first, app_key="app", conversation_key="derived:hello"
        )
        transcript = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi there"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        new_chat_before = client.new_chat_calls
        reset = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        routing = await openai_routes._prepare_conversation_routing(
            client, reset, app_key="app", conversation_key="derived:hello"
        )
        self.assertEqual(routing.action, "new-chat-client-reset")
        self.assertIsNone(routing.previous_route)
        self.assertGreater(client.new_chat_calls, new_chat_before)

    async def test_stale_mapping_is_deleted_and_rebuilt(self) -> None:
        client = _RoutingClient()
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="one")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="conv"
        )
        transcript = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key, app_key=initial.app_key,
            conversation_key=initial.conversation_key, thread_id="missing-thread",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        client.fail_navigation = True
        routing = await openai_routes._prepare_conversation_routing(
            client,
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="two")]),
            app_key="app", conversation_key="conv",
        )
        self.assertEqual(routing.action, "new-chat-stale-mapping")
        self.assertIsNone(openai_routes._get_conversation_store().get_route("global", "app", "conv"))


if __name__ == "__main__":
    unittest.main()
