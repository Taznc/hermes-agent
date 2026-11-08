"""Canonical fork configuration and legacy precedence contracts."""
import pytest

from hermes_fork.config import human_input_bridge


def enabled():
    return {'enabled': True, 'telegram': {'user_id': 42, 'chat_id': 42, 'bot_id': 123456}}


def test_canonical_namespace_activates_and_defaults_inside_profile(tmp_path):
    settings = human_input_bridge({'fork': {'human_input_bridge': enabled()}}, home=tmp_path)
    assert settings['telegram']['user_id'] == 42
    assert settings['delay_seconds'] == 120
    assert settings['mailbox_path'] == tmp_path / 'human-input-bridge' / 'mailbox.sqlite3'


def test_canonical_disabled_or_null_never_falls_through_to_legacy(tmp_path):
    for disabled in ({'enabled': False}, None, {}):
        assert human_input_bridge({'fork': {'human_input_bridge': disabled}, 'human_input_bridge': enabled()}, home=tmp_path) is None


def test_legacy_compatibility_only_when_canonical_absent(tmp_path):
    assert human_input_bridge({'fork': {}, 'human_input_bridge': enabled()}, home=tmp_path)['telegram']['user_id'] == 42
    canonical = enabled()
    canonical['telegram']['user_id'] = 99
    assert human_input_bridge({'fork': {'human_input_bridge': canonical}, 'human_input_bridge': enabled()}, home=tmp_path)['telegram']['user_id'] == 99


@pytest.mark.parametrize('config', [[], 'bad', {'fork': []}, {'fork': {'human_input_bridge': []}}, {'fork': {'human_input_bridge': 'bad'}}, {'fork': {'human_input_bridge': {'enabled': True, 'telegram': 'bad'}}}])
def test_malformed_configuration_is_explicitly_rejected(config, tmp_path):
    with pytest.raises(ValueError):
        human_input_bridge(config, home=tmp_path)
