"""Shared Codex dialogs behind herdr windows; legacy transport remains available."""
import json
import re
import time

from . import codex_app_server as app


def _path(value):
    value = str(value or "").rstrip("/\\")
    return value.replace("\\", "/").casefold() if re.match(r"^[A-Za-z]:", value) else value


def _title(value, cwd):
    title = str(value or "").strip()
    project = str(cwd).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    suffix = " | " + project
    return title[:-len(suffix)] if title.endswith(suffix) else title


def resolve(agent, host=None):
    """Use a session id, or a unique loaded dialog matching the pane. Never guess."""
    if agent.get("agent") != "codex":
        return None
    client = app.get_client(host)
    if client is None:
        return None
    cwd = agent.get("cwd") or agent.get("foreground_cwd")
    if not cwd:
        return None
    session = agent.get("agent_session")
    if isinstance(session, dict):
        session = session.get("value") or session.get("id")
    try:
        explicit = isinstance(session, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", session)
        if explicit:
            thread = client.request("thread/read", {"threadId": session})["thread"]
            candidates = [thread]
        else:
            response = client.request("thread/list", {"cwd": cwd, "limit": 100})
            candidates = response.get("data") or []
            # A truncated listing cannot establish uniqueness.
            if response.get("nextCursor"):
                return None
        candidates = [t for t in candidates
                      if _path(t.get("cwd")) == _path(cwd)
                      and (t.get("status") or {}).get("type") in {"idle", "active"}]
        title = _title(agent.get("terminal_title_stripped"), cwd)
        exact = [t for t in candidates if title and str(t.get("name") or "").strip() == title]
        selected = candidates if explicit else exact
        if len(selected) != 1:
            return None
        thread = selected[0]
        return Dialog(client, thread["id"])
    except (app.Unavailable, app.RPCError, KeyError, TypeError):
        return None


class Dialog:
    def __init__(self, client, thread_id):
        self.client = client
        self.thread_id = thread_id

    def snapshot(self, limit=5):
        thread = self.client.request("thread/read", {"threadId": self.thread_id})["thread"]
        try:
            turns = self.client.request("thread/turns/list", {
                "threadId": self.thread_id, "limit": limit, "itemsView": "summary",
            }).get("data") or []
        except app.RPCError as exc:
            if not any(v in str(exc) for v in ["not materialized yet", "list_turns is not supported", "ephemeral threads"]):
                raise
            turns = list(reversed(self.client.live_turns(self.thread_id)))
        return thread, turns

    def subscribe(self):
        # Replaying pending requests is idempotent, but avoid needless resumes.
        with self.client.lock:
            subscribed = getattr(self.client, "subscriptions", set())
        if self.thread_id not in subscribed:
            metadata = self.client.request("thread/read", {"threadId": self.thread_id})["thread"]
            if (metadata.get("status") or {}).get("type") not in {"idle", "active"}:
                raise app.Unavailable("The Codex dialog is no longer loaded")
            self.client.subscribe(self.thread_id)
            with self.client.lock:
                self.client.subscriptions = subscribed | {self.thread_id}

    def send(self, text):
        thread, turns = self.snapshot()
        if thread.get("canAcceptDirectInput") is not True:
            raise app.Unavailable("The live Codex dialog cannot accept direct input")
        if (thread.get("status") or {}).get("type") not in {"active", "idle"}:
            raise app.Unavailable("The Codex dialog is not loaded")
        self.subscribe()
        if self.client.questions(self.thread_id):
            raise app.RPCError({"message": "Answer the pending Codex question first"})
        active = next((t for t in turns if t.get("status") == "inProgress"), None)
        if (thread.get("status") or {}).get("type") == "active" and active is None:
            raise app.Unavailable("The active Codex turn has not been observed yet")
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]}
        if active:
            params["expectedTurnId"] = active["id"]
            self.client.request("turn/steer", params, mutation=True)
            return active["id"], True
        result = self.client.request("turn/start", params, mutation=True)
        return result["turn"]["id"], False

    def questions(self, turns=None):
        self.subscribe()
        requests = self.client.questions(self.thread_id)
        result = []
        for request in requests:
            for index, question in enumerate(request["params"].get("questions", [])):
                result.append({"kind": "blocking", "request": request, "index": index,
                               "id": question["id"], "text": question["question"],
                               "options": [v["label"] for v in question.get("options") or []]})
        if turns is None:
            _, turns = self.snapshot()
        answered = set()
        async_questions = []
        for turn in reversed(turns):
            for item in turn.get("items") or []:
                if item.get("type") == "userMessage":
                    for content in item.get("content") or []:
                        text = content.get("text", "").strip()
                        start, end = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
                        if text.startswith(start) and text.endswith(end):
                            try:
                                replies = json.loads(text[len(start):-len(end)])
                                for reply in replies if isinstance(replies, list) else [replies]:
                                    answered.add(reply["questionItemId"])
                            except (ValueError, KeyError, TypeError):
                                pass
                elif item.get("type") == "agentMessage" and item.get("questions"):
                    for index, question in enumerate(item["questions"]):
                        identity = json.dumps(["request_user_input_async", item["id"], index], separators=(",", ":"))
                        async_questions.append({"kind": "async", "id": identity,
                                                "text": question["title"],
                                                "options": question.get("options") or []})
        return result + [q for q in async_questions if q["id"] not in answered]

    def answer(self, view, answer, all_answers=None):
        current = self.questions()
        match = next((q for q in current if q == view), None)
        if match is None:
            raise app.Unavailable("The question changed or was already answered")
        if view["kind"] == "blocking":
            answers = dict(all_answers or {})
            answers[view["id"]] = {"answers": [answer]}
            count = len(view["request"]["params"]["questions"])
            if len(answers) < count:
                return answers
            self.client.answer(view["request"]["id"], answers, view["request"])
        else:
            payload = [{"questionItemId": view["id"], "question": view["text"], "answer": answer}]
            return self.send("<send_user_message_question_reply>\n" + json.dumps(payload, ensure_ascii=False)
                             + "\n</send_user_message_question_reply>")
        return None

    def find_turn(self, marker):
        _, turns = self.snapshot(limit=10)
        for turn in turns:
            if any(marker in c.get("text", "") for item in turn.get("items") or []
                   if item.get("type") == "userMessage" for c in item.get("content") or []):
                return turn
        return None

    def read_turn(self, turn_id):
        live = next((t for t in self.client.live_turns(self.thread_id) if t.get("id") == turn_id), None)
        if live:
            return live
        cursor = None
        for _ in range(10):
            try:
                response = self.client.request("thread/turns/list", {
                    "threadId": self.thread_id, "limit": 20, "itemsView": "summary", "cursor": cursor})
            except app.RPCError as exc:
                if any(v in str(exc) for v in ["not materialized yet", "list_turns is not supported", "ephemeral threads"]):
                    return None
                raise
            for turn in response.get("data") or []:
                if turn.get("id") == turn_id:
                    return turn
            cursor = response.get("nextCursor")
            if not cursor:
                break
        return None


def final_text(turn):
    text = "\n\n".join(item.get("text", "") for item in turn.get("items") or []
                         if item.get("type") == "agentMessage"
                         and item.get("phase") == "final_answer" and item.get("delivery") != "async"
                         and not item.get("questions"))
    return re.sub(r"(?m)^PP-REPLY-END:[0-9a-f]{32}\s*$", "", text).rstrip()


class VerifiedReply(str):
    """Final message from a completed, correlated Codex turn."""


class Observation:
    def __init__(self, dialog, marker):
        self.dialog = dialog
        self.marker = marker
        self.turn_id = None

    def poll(self):
        turn = (self.dialog.read_turn(self.turn_id) if self.turn_id
                else self.dialog.find_turn(self.marker))
        if turn:
            self.turn_id = turn["id"]
        return turn


def observe(agent, marker, host=None):
    dialog = resolve(agent, host)
    if dialog:
        try:
            dialog.subscribe()
            return Observation(dialog, marker)
        except (app.Unavailable, app.RPCError):
            pass
    return None


def tracked_turn(dialog, turn_id, pane, machine, host, chat_id, delivery):
    return {"thread_id": dialog.thread_id, "turn_id": turn_id, "pane": pane,
            "machine": machine or "", "endpoint": list(host) if host else None,
            "chat_id": chat_id, "delivery": delivery, "expires_at": time.time() + 86400,
            "done": False}
