"""Flow connectors: GitLab, replies by mail, data on stdin, one item at a time.

What the oneservice and client-letter flows rely on: GitLab labels and
comments land where the team looks; a token never shows on a command line;
a reply goes only to the author of the letter, only after a person saw it;
text from outside reaches a program as data on stdin, never as argv.
"""

import base64
import json
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from promptpilot import config, flow_connectors, flows

PY = sys.executable


@pytest.fixture
def env(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    home = tmp_path / "flows"
    home.mkdir()
    monkeypatch.setenv("PP_FLOWS_DIR", str(home))
    monkeypatch.setenv("GL_TOKEN", "secret-token")
    monkeypatch.setenv("M_USER", "robot@mail.test")
    monkeypatch.setenv("M_PASS", "pw")
    return SimpleNamespace(db=isolated_db, root=tmp_path, flows=home)


def load(flow: dict) -> flows.FlowDef:
    return flows.FlowDef.model_validate(flow)


def finish_task(env, item_id, fields):
    item = flows.get_item(item_id)
    running = env.db.get_next_runnable()
    assert running.id == item["wait"]["task_id"]
    env.db.mark_completed(running.id, flows.RESULT_BEGIN + "\n"
                          + json.dumps(fields, ensure_ascii=False) + "\n" + flows.RESULT_END)


def advance(flow, item_id):
    return flows.advance_item(item_id, {flow.name: flow})


# --- GitLab ------------------------------------------------------------------------------

class FakeGitLab:
    """curl stand-in: records calls, answers like the GitLab API."""

    def __init__(self, issues=()):
        self.issues = {issue["iid"]: dict(issue) for issue in issues}
        self.calls = []
        self.fail = None

    def __call__(self, cmd, data):
        header_file = cmd[cmd.index("-H") + 1].lstrip("@")
        headers = Path(header_file).read_text(encoding="utf-8")
        method, url = cmd[cmd.index("-X") + 1], cmd[-1]
        payload = json.loads(data) if data else None
        self.calls.append({"method": method, "url": url, "payload": payload, "argv": cmd,
                           "headers": headers, "header_file": header_file})
        if self.fail:
            return subprocess.CompletedProcess(cmd, 0, f"{json.dumps(self.fail)}\n403".encode(), b"")
        path = url.split("/projects/", 1)[1].split("/", 1)[1]
        if method == "GET" and path.startswith("issues?"):
            body = list(self.issues.values())
        elif method == "POST" and path == "issues":
            iid = max(self.issues, default=0) + 1
            self.issues[iid] = {"iid": iid, "title": payload["title"], "labels": [],
                                "web_url": f"https://gl.test/i/{iid}"}
            body = self.issues[iid]
        elif method == "PUT":
            iid = int(path.split("/")[1])
            if "labels" in payload:
                self.issues[iid]["labels"] = payload["labels"].split(",")
            body = self.issues[iid]
        else:
            body = {"id": 1}
        return subprocess.CompletedProcess(cmd, 0, (json.dumps(body, ensure_ascii=False) + "\n201").encode(), b"")


def issue(iid, labels=("подано",), title="Не считается итог", body="Итог счёта неверный"):
    return {"iid": iid, "title": title, "description": body, "labels": list(labels),
            "author": {"name": "Коллега"}, "web_url": f"https://gl.test/i/{iid}"}


def team_flow(root: Path, **overrides) -> dict:
    flow = {
        "name": "os",
        "trust": "team",
        "vars": {"repo": str(root / "repo")},
        "gitlab": {"url": "https://gl.test", "token_env": "GL_TOKEN", "project": "g%2Fos"},
        "input": {"type": "gitlab_issues", "labels": ["подано"], "exclude_labels": ["непроверенный отправитель"]},
        "steps": [
            {"id": "start", "kind": "gitlab", "labels": "целесообразность"},
            {"id": "keeper", "kind": "agent", "rights": "none", "provider": "claude",
             "prompt": "Оцени: {{input.title}}\n{{input.body}}",
             "output": {"verdict": {"enum": ["ДА", "НЕТ"]}, "spec": {"type": "text"}}},
            {"id": "note", "kind": "gitlab", "comment": "🧊 {{steps.keeper.verdict}}\n{{steps.keeper.spec}}"},
            {"id": "declined", "kind": "gitlab", "labels": "отклонено",
             "when": {"step": "keeper", "field": "verdict", "in": ["НЕТ"]}},
            {"id": "branch", "kind": "command",
             "run": [PY, "-c", "import sys; print('task/' + sys.argv[1])", "{{input.iid}}"],
             "when": {"step": "keeper", "field": "verdict", "in": ["ДА"]}},
            {"id": "done", "kind": "finish"},
        ],
    }
    flow.update(overrides)
    return flow


@pytest.fixture
def gitlab(monkeypatch):
    fake = FakeGitLab([issue(7), issue(8, labels=("подано", "непроверенный отправитель")),
                       issue(9, labels=("подано",))])
    monkeypatch.setattr(flow_connectors, "_curl", fake)
    return fake


def test_gitlab_issue_goes_through_the_keeper_with_labels_and_notes(env, gitlab):
    flow = load(team_flow(env.root))

    created = flows.poll_input(flow)
    assert sorted(item["data"]["input"]["iid"] for item in created) == [7, 9]  # 8 is unverified
    assert flows.poll_input(flow) == []  # one item per issue
    item = next(item for item in created if item["data"]["input"]["iid"] == 7)

    advance(flow, item["id"])
    assert gitlab.issues[7]["labels"] == ["целесообразность"]
    finish_task(env, item["id"], {"verdict": "да", "spec": "Поправить итог в модуле Расчёт"})
    done = advance(flow, item["id"])

    assert done["status"] == "done"
    notes = [call for call in gitlab.calls if call["url"].endswith("/issues/7/notes")]
    assert notes[0]["payload"]["body"] == "🧊 ДА\nПоправить итог в модуле Расчёт"
    assert done["data"]["steps"]["branch"]["output"].strip() == "task/7"
    assert gitlab.issues[7]["labels"] == ["целесообразность"]  # «отклонено» skipped


def test_gitlab_token_never_reaches_the_command_line(env, gitlab):
    flow = load(team_flow(env.root))

    flows.poll_input(flow)

    call = gitlab.calls[0]
    assert "secret-token" not in " ".join(call["argv"])
    assert "PRIVATE-TOKEN: secret-token" in call["headers"]
    assert not Path(call["header_file"]).exists()  # removed after the call
    assert "labels=%D0%BF%D0%BE%D0%B4%D0%B0%D0%BD%D0%BE" in call["url"]


def test_gitlab_refusal_hands_the_item_to_a_person(env, gitlab):
    flow = load(team_flow(env.root))
    item = flows.poll_input(flow)[0]
    gitlab.fail = {"message": "403 Forbidden"}

    stuck = advance(flow, item["id"])

    assert stuck["status"] == "needs_human"
    assert "HTTP 403" in stuck["error"]


def test_issue_iid_is_fixed_but_its_text_is_not(env):
    flow = team_flow(env.root, trust="client")
    flow["steps"][4]["run"] = [PY, "-c", "print(1)", "{{input.title}}"]
    flow["steps"][1]["rights"] = "read"

    with pytest.raises(ValueError, match="текст извне"):
        load(flow)
    flow["steps"][4]["run"] = [PY, "-c", "print(1)", "{{input.iid}}"]
    assert load(flow).name == "os"


def test_gitlab_step_needs_a_connection_and_an_issue(env):
    no_connection = team_flow(env.root)
    del no_connection["gitlab"]
    no_issue = team_flow(env.root)
    no_issue["input"] = None

    with pytest.raises(ValueError, match="не описан gitlab"):
        load(no_connection)
    with pytest.raises(ValueError, match="какой issue"):
        load(no_issue)


def test_labels_of_a_client_flow_cannot_come_from_the_request(env):
    flow = team_flow(env.root, trust="client")
    flow["steps"][1]["rights"] = "read"
    flow["steps"][0]["labels"] = "{{input.title}}"

    with pytest.raises(ValueError, match="текст извне"):
        load(flow)


# --- mail: input flags, replies, attachments -----------------------------------------------

class FakeIMAP:
    def __init__(self, messages):
        self.raw = [message.as_bytes() for message in messages]

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.raw))).encode()]

    def fetch(self, num, parts):
        raw = self.raw[int(num) - 1]
        if "HEADER" in parts:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1", raw)]

    def logout(self):
        pass


def letter(sender, body="Не работает выгрузка", subject="[OS] Выгрузка", reply_to=None,
           attachment=None, key=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = key or f"<{abs(hash((sender, body, subject)))}@test>"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="octet-stream",
                           filename=attachment[0])
    return msg


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=30):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def mail_flow(root, trust="client", **input_overrides):
    return {
        "name": "mail",
        "trust": trust,
        "vars": {"signature": "С уважением, Иван"},
        "input": {"type": "email", "host": "imap.test", "user_env": "M_USER", "password_env": "M_PASS",
                  "subject_marker": "OS", "allow_from": ["known.ru"], "allow_from_mode": "mark",
                  **input_overrides},
        "steps": [
            {"id": "flag", "kind": "command", "when": {"input": "sender_allowed", "in": [False]},
             "run": [PY, "-c", "print('unverified', __import__('sys').argv[1])", "{{input.sender_allowed}}"]},
            {"id": "approve", "kind": "human", "text": "Ответить?", "show": ["input.body"]},
            {"id": "reply", "kind": "email_reply", "text": "Спасибо, посмотрим.\n{{flow.signature}}",
             "text_override": "{{steps.approve.note}}", "smtp_host": "smtp.test"},
            {"id": "done", "kind": "finish"},
        ],
    }


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(flow_connectors.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_unlisted_sender_is_taken_but_marked(env):
    flow = load(mail_flow(env.root))
    mailbox = FakeIMAP([letter("Друг <a@known.ru>"), letter("Чужой <x@evil.test>")])

    created = flows.poll_input(flow, connect=lambda *a: mailbox)

    assert [item["data"]["input"]["sender_allowed"] for item in created] == [True, False]
    advance(flow, created[1]["id"])
    assert flows.get_item(created[1]["id"])["data"]["steps"]["flag"]["output"].strip() == "unverified false"
    advance(flow, created[0]["id"])
    assert "flag" not in flows.get_item(created[0]["id"])["data"]["steps"]


# An encoded word may decode to a line break: "Bcc:" smuggled into the reply's headers.
SMUGGLED_SUBJECT = "=?utf-8?b?" + base64.b64encode(
    "[OS] Re: Выгрузка\r\nBcc: victim@x".encode()).decode() + "?="


def test_reply_goes_to_the_author_after_a_person_approves(env, smtp):
    flow = load(mail_flow(env.root))
    mailbox = FakeIMAP([letter("Форма <robot@form.test>", reply_to="Автор <author@client.test>",
                               subject=SMUGGLED_SUBJECT, key="<m-1@test>")])
    item = flows.poll_input(flow, connect=lambda *a: mailbox)[0]
    advance(flow, item["id"])
    assert smtp.sent == []  # nothing leaves before the decision

    flows.decide(item["id"], "approve", note="", flows={flow.name: flow})
    done = advance(flow, item["id"])

    assert done["status"] == "done"
    message = smtp.sent[0]
    assert message["To"] == "author@client.test"
    assert message["In-Reply-To"] == "<m-1@test>"
    assert "\n" not in message["Subject"] and "Bcc" not in message.keys()
    assert "С уважением, Иван" in message.get_content()


def test_the_persons_own_text_replaces_the_draft(env, smtp):
    flow = load(mail_flow(env.root))
    item = flows.poll_input(flow, connect=lambda *a: FakeIMAP([letter("a@known.ru")]))[0]
    advance(flow, item["id"])

    flows.decide(item["id"], "approve", note="Исправлено, проверьте, пожалуйста.",
                 flows={flow.name: flow})
    advance(flow, item["id"])

    assert smtp.sent[0].get_content().strip() == "Исправлено, проверьте, пожалуйста."


def test_nobody_to_answer_is_a_stop_not_a_letter_to_the_form(env, smtp):
    flow = load(mail_flow(env.root, require_from_domain="form.test", allow_from=[]))
    item = flows.poll_input(flow, connect=lambda *a: FakeIMAP([letter("robot@form.test")]))[0]
    advance(flow, item["id"])
    flows.decide(item["id"], "approve", flows={flow.name: flow})

    stuck = advance(flow, item["id"])

    assert stuck["status"] == "needs_human" and "адреса автора" in stuck["error"]
    assert smtp.sent == []


def test_a_client_flow_does_not_mail_before_a_person_decides(env):
    flow = mail_flow(env.root)
    flow["steps"] = [step for step in flow["steps"] if step["id"] != "approve"]
    for step in flow["steps"]:
        step.pop("text_override", None)

    with pytest.raises(ValueError, match="только после согласования"):
        load(flow)


def test_reply_needs_a_letter_to_reply_to(env):
    flow = mail_flow(env.root, trust="owner")
    flow["input"] = None
    flow["steps"][0].pop("when")
    flow["steps"][0]["run"] = [PY, "-c", "print(1)"]

    with pytest.raises(ValueError, match="только на письмо"):
        load(flow)


def test_unknown_input_flag_is_refused(env):
    flow = mail_flow(env.root)
    flow["steps"][0]["when"] = {"input": "vip", "in": [True]}

    with pytest.raises(ValueError, match="нет флага «vip»"):
        load(flow)


def test_attachments_land_in_the_items_own_folder(env):
    flow = mail_flow(env.root, save_attachments=True)
    flow["steps"].insert(0, {"id": "look", "kind": "agent", "rights": "read", "provider": "claude",
                             "working_dir": "{{flow.attachments}}/{{item.id}}",
                             "prompt": "Вложения: {{input.attachments}}"})
    flow = load(flow)
    mailbox = FakeIMAP([letter("a@known.ru", attachment=("../../evil.bat", b"payload"))])

    item = flows.poll_input(flow, connect=lambda *a: mailbox)[0]
    advance(flow, item["id"])

    paths = flows.get_item(item["id"])["data"]["input"]["attachments"]
    folder = flows.attachments_dir(flow) / str(item["id"])
    assert [Path(path).parent for path in paths] == [folder]
    assert Path(paths[0]).name == "01-evil.bat" and Path(paths[0]).read_bytes() == b"payload"
    task = env.db.get_task(flows.get_item(item["id"])["wait"]["task_id"])
    assert Path(task.working_dir) == folder


# --- commands: data on stdin -------------------------------------------------------------

def test_outside_text_reaches_a_command_on_stdin_only(env):
    flow = load({
        "name": "stdin", "trust": "public",
        "steps": [
            {"id": "count", "kind": "command",
             "run": [PY, "-c", "import sys; data = sys.stdin.read(); print(len(sys.argv), data.upper())"],
             "stdin": "{{input.body}}"},
            {"id": "done", "kind": "finish"},
        ],
    })
    item = flows.create_item(flow, {"body": "rm -rf / ; $(whoami)"}, title="x")

    done = advance(flow, item["id"])

    assert done["data"]["steps"]["count"]["output"].strip() == "1 RM -RF / ; $(WHOAMI)"


# --- one item at a time ----------------------------------------------------------------------

def test_exclusive_section_lets_one_item_in_at_a_time(env):
    flow = load({
        "name": "one", "trust": "owner",
        "exclusive": {"first": "work", "last": "check"},
        "steps": [
            {"id": "work", "kind": "command", "run": [PY, "-c", "print('work')"]},
            {"id": "check", "kind": "human", "text": "Готово?"},
            {"id": "done", "kind": "finish"},
        ],
    })
    first = flows.create_item(flow, {}, title="first")
    second = flows.create_item(flow, {}, title="second")

    assert advance(flow, first["id"])["status"] == "waiting_human"  # inside, holds it
    waiting = advance(flow, second["id"])
    assert waiting["status"] == "active" and "work" not in waiting["data"]["steps"]

    flows.decide(first["id"], "approve", flows={flow.name: flow})
    assert advance(flow, first["id"])["status"] == "done"
    assert advance(flow, second["id"])["status"] == "waiting_human"


# --- a project catalogue that follows the disk --------------------------------------------------

def test_enum_from_project_folders(env):
    projects = env.root / "projects"
    for name in ("Обмены/Загрузчик", "Сайт", ".git"):
        (projects / name).mkdir(parents=True)
    flow = load({
        "name": "catalog", "trust": "client",
        "vars": {"projects": str(projects)},
        "steps": [
            {"id": "triage", "kind": "agent", "rights": "read", "provider": "claude", "prompt": "?",
             "output": {"project": {"enum": ["НЕТ"], "enum_dirs": "{{flow.projects}}", "dirs_depth": 2}}},
            {"id": "approve", "kind": "human", "text": "Запускать?"},
            {"id": "work", "kind": "agent", "rights": "write", "provider": "claude", "prompt": "делай",
             "working_dir": "{{flow.projects}}/{{steps.triage.project}}",
             "when": {"step": "triage", "field": "project", "not_in": ["НЕТ"]}},
        ],
    })

    values = flow.steps[0].output["project"].values
    assert values == ["НЕТ", "Обмены", "Обмены/Загрузчик", "Сайт"]
    with pytest.raises(flows.FlowError, match="нет среди"):
        flows.parse_result(f"{flows.RESULT_BEGIN}\n{{\"project\": \"../../Windows\"}}\n{flows.RESULT_END}",
                           flow.steps[0].output)


def test_enum_from_a_missing_folder_is_an_error_of_the_file(env):
    with pytest.raises(ValueError, match="не прочитать"):
        load({"name": "catalog", "trust": "owner", "vars": {"projects": str(env.root / "nope")},
              "steps": [{"id": "triage", "kind": "agent", "provider": "claude", "prompt": "?",
                         "output": {"project": {"enum_dirs": "{{flow.projects}}"}}}]})


# --- the examples --------------------------------------------------------------------------------

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples" / "flows"


@pytest.mark.parametrize("name", ["oneservice", "oneservice-mail", "client-letters"])
def test_examples_are_valid(env, name):
    raw = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    if "projects" in raw.get("vars", {}):
        projects = env.root / "projects"
        (projects / "Сайт").mkdir(parents=True)
        raw["vars"]["projects"] = str(projects)

    flow = flows.FlowDef.model_validate({**raw, "source_dir": str(EXAMPLES)})

    for step in flow.steps:
        if getattr(step, "prompt_file", ""):
            assert (EXAMPLES / step.prompt_file).is_file()
