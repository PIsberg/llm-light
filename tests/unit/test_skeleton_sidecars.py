"""Generated sidecar files are left out of the skeleton tree (#351).

Godot 4 writes a <file>.uid beside every script and scene and a <file>.import beside
every imported asset; Unity writes a .meta beside every file and folder. Each only
repeats a name the tree already lists: in one Godot game they were 40% of a
~15,000-token tree. A sidecar whose own file or folder is listed beside it is dropped,
and one line says how many were.
"""

from pathlib import Path

import pytest

from llm_light import skeleton


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def build(*names, dirs=()):
        for d in dirs:
            Path(d).mkdir(parents=True, exist_ok=True)
        for name in names:
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            Path(name).write_text("x", encoding="utf-8")
        paths = [Path(n) for n in names] + [Path(d) for d in dirs]
        return skeleton._build_compressed_tree(paths)
    return build


def test_godot_sidecars_beside_their_files_are_left_out(tree):
    out = tree("scripts/player.gd", "scripts/player.gd.uid", "scripts/enemy.gd", "scripts/enemy.gd.uid",
               "art/icon.png", "art/icon.png.import")
    assert "player.gd.uid" not in out and "icon.png.import" not in out and "{enemy,player}.gd.uid" not in out
    assert "player" in out and "icon.png" in out


def test_the_tree_says_how_many_were_left_out(tree):
    out = tree("a.gd", "a.gd.uid", "b.gd", "b.gd.uid", "c.png", "c.png.import")
    assert "3 generated" in out


def test_unity_meta_files_for_files_and_folders_are_left_out(tree):
    out = tree("Assets/Player.cs", "Assets/Player.cs.meta", "Assets/Scenes.meta", dirs=("Assets/Scenes",))
    assert ".meta" not in out.split("Not listed")[0]
    assert "Player.cs" in out and "Scenes/" in out


def test_a_sidecar_without_its_file_stays(tree):
    # Nothing else names it, so dropping it would hide a file.
    out = tree("orphan.gd.uid", "main.gd")
    assert "orphan.gd.uid" in out


def test_a_tree_without_sidecars_is_unchanged(tree):
    out = tree("src/a.py", "src/b.py", "README.md")
    assert "Not listed" not in out and "{a,b}.py" in out
