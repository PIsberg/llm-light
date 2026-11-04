"""@shot.png: images go with a question, in the way each provider can read them (#207)."""

import base64
from unittest.mock import MagicMock, patch

import pytest

import llm_light.state as state
from llm_light import commands, llm
from llm_light.providers import claude, codex, gemini

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
GIF = b"GIF89a" + b"\0" * 32
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 32


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _attach(text, provider=claude.PROVIDER):
    with patch.object(commands.providers, "active", return_value=provider):
        return commands.attach_images(text)


# --- which files count ---------------------------------------------------------------

@pytest.mark.parametrize("name, data, media_type", [("a.png", PNG, "image/png"), ("a.jpg", JPEG, "image/jpeg"),
                                                    ("a.jpeg", JPEG, "image/jpeg"), ("a.gif", GIF, "image/gif"),
                                                    ("a.webp", WEBP, "image/webp"), ("A.PNG", PNG, "image/png")])
def test_each_format_is_attached_with_its_type(project, name, data, media_type):
    (project / name).write_bytes(data)
    images, problems = _attach(f"what is @{name}")
    assert problems == []
    assert images == [{"path": name, "media_type": media_type, "data": base64.b64encode(data).decode("ascii")}]


def test_the_type_comes_from_the_bytes_not_the_name(project):
    """The API refuses a declared type that does not match the bytes."""
    (project / "really_a.png").write_bytes(JPEG)
    images, _ = _attach("@really_a.png")
    assert images[0]["media_type"] == "image/jpeg"


def test_a_file_named_like_an_image_that_is_not_one_is_refused(project):
    (project / "fake.png").write_bytes(b"not an image at all")
    images, problems = _attach("@fake.png")
    assert images == [] and "not an image" in problems[0]


def test_outside_the_project_is_refused(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.png"
    outside.write_bytes(PNG)
    images, problems = _attach(f"@{outside}")
    assert images == [] and "outside the project" in problems[0]


def test_a_relative_path_out_of_the_project_is_refused(project):
    (project.parent / "up.png").write_bytes(PNG)
    images, problems = _attach("@../up.png")
    assert images == [] and "outside the project" in problems[0]


def test_too_large_is_refused_before_it_is_sent(project):
    (project / "big.png").write_bytes(PNG + b"\0" * commands.IMAGE_MAX_BYTES)
    images, problems = _attach("@big.png")
    assert images == [] and "MB" in problems[0]


def test_too_many_are_cut_and_say_so(project):
    for i in range(commands.IMAGE_MAX_COUNT + 2):
        (project / f"i{i}.png").write_bytes(PNG)
    text = " ".join(f"@i{i}.png" for i in range(commands.IMAGE_MAX_COUNT + 2))
    images, problems = _attach(text)
    assert len(images) == commands.IMAGE_MAX_COUNT
    assert "at most" in problems[0]


def test_the_same_image_twice_goes_once(project):
    (project / "a.png").write_bytes(PNG)
    images, _ = _attach("@a.png and again @a.png")
    assert len(images) == 1


def test_text_files_are_left_to_expand_mentions(project):
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert _attach("@a.py") == ([], [])


def test_no_mention_is_nothing(project):
    assert _attach("just a question about me@example.com") == ([], [])


@pytest.mark.parametrize("provider", [codex.PROVIDER, gemini.PROVIDER])
def test_a_provider_without_tools_says_it_cannot(project, provider):
    (project / "a.png").write_bytes(PNG)
    images, problems = _attach("@a.png", provider)
    assert images == []
    assert provider.name in problems[0] and "cannot read images" in problems[0]


def test_an_image_is_no_longer_reported_as_a_binary_file(project):
    (project / "a.png").write_bytes(PNG)
    context, attached, problems = commands.expand_mentions("@a.png")
    assert (context, attached, problems) == ("", [], [])


def test_other_binaries_still_are(project):
    (project / "a.bin").write_bytes(b"\0\0\0")
    assert "binary" in commands.expand_mentions("@a.bin")[2][0]


# --- the request ---------------------------------------------------------------------

_IMAGE = {"path": "a.png", "media_type": "image/png", "data": "QUJD"}


def _chat_request(oauth, **kwargs):
    seen = {}
    provider = MagicMock()
    provider.has.return_value = True

    def complete(request):
        seen.update(request)
        return "reply", MagicMock(), True

    provider.complete.side_effect = complete
    with patch.object(llm.providers, "active", return_value=provider), \
            patch.object(llm, "_agent_oauth", return_value=oauth), \
            patch.object(llm, "_finalize_route", return_value=("m", "low", 100)), \
            patch.object(llm, "route_query", return_value=("m", "low", 100)), \
            patch.object(llm, "_record_usage", return_value=False), \
            patch.object(llm, "_git_modified_snapshot", return_value=set()), \
            patch.object(llm, "print_stats"):
        assert llm.chat("what is this?", use_index=False, **kwargs) is True
    return seen


@pytest.fixture(autouse=True)
def _clean_history():
    state.conversation_history.clear()
    yield
    state.conversation_history.clear()
    state.checkpoints.clear()


def test_api_mode_sends_an_image_block_after_the_question():
    request = _chat_request(False, images=[_IMAGE])
    blocks = request["messages"][-1]["content"]
    assert blocks[-1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert blocks[-2]["type"] == "text" and "what is this?" in blocks[-2]["text"]


def test_a_turn_with_no_image_is_unchanged():
    request = _chat_request(False)
    assert all(block["type"] == "text" for block in request["messages"][-1]["content"])


def test_the_history_keeps_the_path_not_the_bytes():
    _chat_request(False, images=[_IMAGE])
    stored = state.conversation_history[0]["content"]
    assert "a.png" in stored and "QUJD" not in stored


def test_the_subscription_gets_the_path_to_read():
    request = _chat_request(True, images=[_IMAGE])
    blocks = request["messages"][-1]["content"]
    assert all(block["type"] == "text" for block in blocks)
    assert "a.png" in blocks[-1]["text"] and "Read" in blocks[-1]["text"]
    assert "QUJD" not in blocks[-1]["text"]


def test_one_shot_sends_it_too():
    seen = {}
    provider = MagicMock()
    provider.has.return_value = True

    def complete(request):
        seen.update(request)
        return "reply", MagicMock(), True

    provider.complete.side_effect = complete
    with patch.object(llm.providers, "active", return_value=provider), \
            patch.object(llm, "_agent_oauth", return_value=False), \
            patch.object(llm, "_finalize_route", return_value=("m", "low", 100)), \
            patch.object(llm, "route_query", return_value=("m", "low", 100)), \
            patch.object(llm, "_record_usage", return_value=False):
        llm._one_shot_turn("what is this?", False, use_index=False, images=[_IMAGE])
    content = seen["messages"][-1]["content"]
    assert content[-1]["type"] == "image" and "what is this?" in content[-2]["text"]


def test_one_shot_without_an_image_still_sends_a_string():
    """The request a one-shot turn has always sent, byte for byte."""
    seen = {}
    provider = MagicMock()
    provider.has.return_value = True

    def complete(request):
        seen.update(request)
        return "reply", MagicMock(), True

    provider.complete.side_effect = complete
    with patch.object(llm.providers, "active", return_value=provider), \
            patch.object(llm, "_agent_oauth", return_value=False), \
            patch.object(llm, "_finalize_route", return_value=("m", "low", 100)), \
            patch.object(llm, "route_query", return_value=("m", "low", 100)), \
            patch.object(llm, "_record_usage", return_value=False):
        llm._one_shot_turn("q", False, use_index=False)
    assert isinstance(seen["messages"][-1]["content"], str)


# --- the prompt ----------------------------------------------------------------------

def test_ask_attaches_and_names_the_image(project, capsys):
    from llm_light import main as m
    (project / "a.png").write_bytes(PNG)
    with patch.object(m, "chat", return_value=True) as chat, patch.object(m, "_index_ready_for_query", return_value=True), \
            patch.object(m.session, "save"), patch.object(commands.providers, "active", return_value=claude.PROVIDER):
        m._ask("what is @a.png")
    assert chat.call_args.kwargs["images"][0]["path"] == "a.png"
    assert "a.png" in capsys.readouterr().out


def test_ask_without_an_image_calls_chat_as_before(project):
    from llm_light import main as m
    with patch.object(m, "chat", return_value=True) as chat, patch.object(m, "_index_ready_for_query", return_value=True), \
            patch.object(m.session, "save"):
        m._ask("a question")
    assert "images" not in chat.call_args.kwargs


def test_ask_with_mentions_off_attaches_no_image(project):
    """/run sends a command's output: an "@x.png" in it is not the user attaching a file."""
    from llm_light import main as m
    (project / "a.png").write_bytes(PNG)
    with patch.object(m, "chat", return_value=True) as chat, patch.object(m, "_index_ready_for_query", return_value=True), \
            patch.object(m.session, "save"):
        m._ask("output said @a.png", mentions=False)
    assert "images" not in chat.call_args.kwargs
