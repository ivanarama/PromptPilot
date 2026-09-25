from click.testing import CliRunner

from promptpilot.cli import cli
from promptpilot.version import __version__, _BUILD_META, full_version


def test_full_version_contains_package_version():
    assert full_version().startswith(__version__)


def test_build_meta_only_from_stamped_module():
    # In a source checkout (and CI) promptpilot/_build_commit.py does not
    # exist and the meta stays empty; a release build may fill commit+label.
    assert set(_BUILD_META) <= {"commit", "label"}


def test_cli_version_option_works():
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
