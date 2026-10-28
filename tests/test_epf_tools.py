"""Тесты 1С-доработки (epf_tools): конфиг, имена, базы."""

from pathlib import Path

import pytest

from promptpilot import epf_tools


# ── Имена проектов ────────────────────────────────────────────────────────

class TestNames:
    def test_sanitize_strips_extension(self):
        assert epf_tools.sanitize_project_name("Моя Обработка.epf") == "Моя Обработка"

    def test_sanitize_replaces_invalid_chars(self):
        assert epf_tools.sanitize_project_name('a<b>:"c.epf') == "a_b___c"

    def test_sanitize_empty_becomes_default(self):
        assert epf_tools.sanitize_project_name("...") == "Обработка"

    @pytest.mark.parametrize("bad", [
        "../etc", "..", ".", ".hidden", "", "a/b", "a\\b", "C:\\temp",
    ])
    def test_safe_project_name_rejects_traversal(self, bad):
        assert epf_tools.safe_project_name(bad) is None

    def test_safe_project_name_accepts_simple(self):
        assert epf_tools.safe_project_name("МояОбработка") == "МояОбработка"

    def test_unique_project_dir_suffixes(self, tmp_path: Path):
        first = epf_tools.unique_project_dir(tmp_path, "Обработка")
        first.mkdir()
        second = epf_tools.unique_project_dir(tmp_path, "Обработка")
        assert first != second
        assert second.name == "Обработка_2"


# ── Категоризация баз ─────────────────────────────────────────────────────

class TestCategorize:
    def test_ut11_from_real_dir_names(self):
        assert epf_tools.categorize_base_name("УТПустаяДляСборки11_5_25_80") == "УТ 11"

    def test_bp3_erp_unf(self):
        assert epf_tools.categorize_base_name("БППустаяДляСборки3_0") == "БП 3.0"
        assert epf_tools.categorize_base_name("ERPДемо2_4_12") == "ERP"
        assert epf_tools.categorize_base_name("УНФ3_0_6_145") == "УНФ"

    def test_unknown(self):
        assert epf_tools.categorize_base_name("КакаятоБаза") == "Другое"


# ── Конфигурация: PP_1C_* env → tg_config.json ───────────────────────────

class TestConfig:
    def test_env_overrides_file(self, tmp_path: Path, monkeypatch):
        cfg_file = tmp_path / "tg_config.json"
        cfg_file.write_text(
            '{"1c": {"exe_path": "C:/from-file/1cv8.exe"}}', encoding="utf-8"
        )
        monkeypatch.setattr(epf_tools, "_tg_config_file", lambda: cfg_file)
        monkeypatch.setenv("PP_1C_EXE_PATH", "C:/from-env/1cv8.exe")
        assert epf_tools.load_1c_config()["exe_path"] == "C:/from-env/1cv8.exe"

    def test_file_section_used_without_env(self, tmp_path: Path, monkeypatch):
        cfg_file = tmp_path / "tg_config.json"
        cfg_file.write_text(
            '{"1c": {"exe_path": "C:/from-file/1cv8.exe", "ut11": "C:/ut"}}',
            encoding="utf-8",
        )
        monkeypatch.setattr(epf_tools, "_tg_config_file", lambda: cfg_file)
        monkeypatch.delenv("PP_1C_EXE_PATH", raising=False)
        cfg = epf_tools.load_1c_config()
        assert cfg["exe_path"] == "C:/from-file/1cv8.exe"
        assert cfg["presets"]["ut11"] == "C:/ut"

    def test_missing_file_is_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            epf_tools, "_tg_config_file", lambda: tmp_path / "nope.json"
        )
        for key in ("PP_1C_EXE_PATH", "PP_1C_BASES_ROOT", "PP_1C_UT_BASE"):
            monkeypatch.delenv(key, raising=False)
        cfg = epf_tools.load_1c_config()
        assert cfg["exe_path"] == ""
        assert cfg["presets"]["ut11"] == ""


# ── Сохранение настроек 1С (tg_config.json) ──────────────────────────────

class TestSaveConfig:
    def test_creates_section_and_preserves_other_keys(self, tmp_path: Path, monkeypatch):
        cfg_file = tmp_path / "tg_config.json"
        cfg_file.write_text(
            '{"allowed_phones": ["+79001234567"]}', encoding="utf-8"
        )
        monkeypatch.setattr(epf_tools, "_tg_config_file", lambda: cfg_file)
        epf_tools.save_1c_config({"exe_path": "C:/1cv8.exe", "ut11": "C:/ut"})
        data = __import__("json").loads(cfg_file.read_text("utf-8"))
        assert data["allowed_phones"] == ["+79001234567"]
        assert data["1c"] == {"exe_path": "C:/1cv8.exe", "ut11": "C:/ut"}

    def test_merges_and_ignores_unknown_keys(self, tmp_path: Path, monkeypatch):
        cfg_file = tmp_path / "tg_config.json"
        cfg_file.write_text('{"1c": {"ut11": "C:/old"}}', encoding="utf-8")
        monkeypatch.setattr(epf_tools, "_tg_config_file", lambda: cfg_file)
        epf_tools.save_1c_config({"ut11": "C:/new", "hacker": "x", "aa": ""})
        data = __import__("json").loads(cfg_file.read_text("utf-8"))
        assert data["1c"] == {"ut11": "C:/new"}

    def test_no_tmp_files_left(self, tmp_path: Path, monkeypatch):
        cfg_file = tmp_path / "tg_config.json"
        monkeypatch.setattr(epf_tools, "_tg_config_file", lambda: cfg_file)
        epf_tools.save_1c_config({"exe_path": "C:/1cv8.exe"})
        assert list(tmp_path.iterdir()) == [cfg_file]

    def test_env_overridden_keys(self, monkeypatch):
        import os
        for k in [k for k in os.environ if k.startswith("PP_1C_")]:
            monkeypatch.delenv(k, raising=False)
        assert epf_tools.env_overridden_keys() == []
        monkeypatch.setenv("PP_1C_EXE_PATH", "x")
        monkeypatch.setenv("PP_1C_UT_BASE", "y")
        keys = epf_tools.env_overridden_keys()
        assert "exe_path" in keys and "ut11" in keys


# ── Разрешение баз ────────────────────────────────────────────────────────

class TestResolveBase:
    def test_stub_resolves_to_none(self):
        path, label = epf_tools.resolve_base("stub")
        assert path is None
        assert "Stub" in label

    def test_empty_resolves_to_stub(self):
        path, _ = epf_tools.resolve_base(None)
        assert path is None

    def test_unknown_key_warns(self):
        path, label = epf_tools.resolve_base("нет_такой_базы")
        assert path is None
        assert "Не найдена" in label

    def test_direct_path(self, tmp_path: Path):
        base = tmp_path / "ТестоваяБаза"
        base.mkdir()
        (base / "1Cv8.1CD").write_bytes(b"")
        path, label = epf_tools.resolve_base(str(base))
        assert path == str(base)
        assert "ТестоваяБаза" in label

    def test_scan_finds_nested_bases(self, tmp_path: Path, monkeypatch):
        nested = tmp_path / "ДляСборки" / "БП3"
        nested.mkdir(parents=True)
        (nested / "1Cv8.1CD").write_bytes(b"")
        monkey_root = tmp_path
        import promptpilot.epf_tools as et
        monkey_cfg = {"bases_root": str(monkey_root), "base_path": "",
                      "exe_path": "", "presets": {}, "skills_dir": "",
                      "skills_repo": ""}
        monkeypatch.setattr(et, "load_1c_config", lambda: monkey_cfg)
        bases = et.scan_available_bases()
        assert [b["name"] for b in bases] == ["ДляСборки/БП3"]
        assert bases[0]["category"] == "БП 3.0"
