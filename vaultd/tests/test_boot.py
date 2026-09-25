"""Boot refusals and the ob setup sequence (plan sections 5.2, 15)."""

from __future__ import annotations

import pytest

from vaultd import boot
from tests.conftest import OWNED_ID, SHARED_ID, boot_config, remote_listing


def _ready(fake_ob, data_dir, *, owned=(("Anchor", OWNED_ID),), shared=(), local=()) -> None:
    fake_ob.set("sync-list-remote", stdout=remote_listing(owned=owned, shared=shared))
    fake_ob.set("sync-list-local", stdout={"vaults": [{"id": i, "path": p, "host": "h"} for i, p in local]})
    fake_ob.set("sync-setup", stdout={"vaultId": OWNED_ID})
    fake_ob.set("sync-config", stdout={})


async def _boot(boot_env, fake_ob):
    return await boot.boot(boot_config(boot_env), boot_env, str(fake_ob.bin))


def _assert_no_values(message: str, env: dict[str, str]) -> None:
    for name in ("VAULT_API_TOKEN", "OBSIDIAN_AUTH_TOKEN", "OBSIDIAN_E2EE_PASSWORD"):
        value = env.get(name)
        if value:
            assert value not in message


@pytest.mark.parametrize("var", ["RAILWAY_PUBLIC_DOMAIN", "RAILWAY_TCP_PROXY_DOMAIN"])
async def test_refuses_a_public_endpoint(boot_env, fake_ob, data_dir, var) -> None:
    _ready(fake_ob, data_dir)
    boot_env[var] = "vault-production.up.example.net"
    with pytest.raises(SystemExit) as exc:
        await _boot(boot_env, fake_ob)
    assert var in str(exc.value)
    assert "vault-production" not in str(exc.value)
    assert fake_ob.calls() == []


async def test_refuses_a_short_token(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir)
    boot_env["VAULT_API_TOKEN"] = "short-token-value"
    with pytest.raises(SystemExit) as exc:
        await _boot(boot_env, fake_ob)
    assert "VAULT_API_TOKEN" in str(exc.value)
    assert "short-token-value" not in str(exc.value)
    assert fake_ob.calls() == []


@pytest.mark.parametrize("var", ["OBSIDIAN_AUTH_TOKEN", "OBSIDIAN_VAULT", "OBSIDIAN_E2EE_PASSWORD"])
@pytest.mark.parametrize("value", [None, "", "   "])
async def test_refuses_a_missing_obsidian_variable(boot_env, fake_ob, data_dir, var, value) -> None:
    _ready(fake_ob, data_dir)
    if value is None:
        del boot_env[var]
    else:
        boot_env[var] = value
    with pytest.raises(SystemExit) as exc:
        await _boot(boot_env, fake_ob)
    assert var in str(exc.value)
    _assert_no_values(str(exc.value), boot_env)
    assert fake_ob.calls() == []


@pytest.mark.parametrize("wanted", ["Shared vault", SHARED_ID])
async def test_refuses_a_vault_shared_with_you(boot_env, fake_ob, data_dir, wanted) -> None:
    _ready(fake_ob, data_dir, shared=(("Shared vault", SHARED_ID),))
    boot_env["OBSIDIAN_VAULT"] = wanted
    with pytest.raises(SystemExit) as exc:
        await _boot(boot_env, fake_ob)
    assert "shared" in str(exc.value)
    assert fake_ob.commands() == ["sync-list-remote"]


async def test_refuses_a_name_that_is_both_yours_and_shared(boot_env, fake_ob, data_dir) -> None:
    """Fail closed: a name that could mean a shared vault means no vault."""
    _ready(fake_ob, data_dir, shared=(("Anchor", SHARED_ID),))
    with pytest.raises(SystemExit):
        await _boot(boot_env, fake_ob)
    assert "sync-setup" not in fake_ob.commands()


async def test_an_owned_id_wins_even_if_a_shared_vault_has_the_same_name(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir, shared=(("Anchor", SHARED_ID),))
    boot_env["OBSIDIAN_VAULT"] = OWNED_ID
    await _boot(boot_env, fake_ob)
    assert "sync-setup" in fake_ob.commands()


@pytest.mark.parametrize("owned", [(), (("Anchor", "id1"), ("Anchor", "id2"))])
async def test_refuses_an_unknown_or_ambiguous_vault(boot_env, fake_ob, data_dir, owned) -> None:
    _ready(fake_ob, data_dir, owned=owned)
    with pytest.raises(SystemExit):
        await _boot(boot_env, fake_ob)
    assert fake_ob.commands() == ["sync-list-remote"]


async def test_refuses_when_the_path_is_linked_to_another_vault(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir, local=(("someone-elses", str(data_dir / "vault")),))
    with pytest.raises(SystemExit) as exc:
        await _boot(boot_env, fake_ob)
    assert "linked" in str(exc.value)
    assert fake_ob.commands() == ["sync-list-remote", "sync-list-local"]


async def test_refuses_when_ob_fails(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir)
    fake_ob.set("sync-list-remote", exit=2, stderr="No account logged in.")
    with pytest.raises(SystemExit):
        await _boot(boot_env, fake_ob)


async def test_first_boot_sets_up_then_configures(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir)
    env = await _boot(boot_env, fake_ob)
    assert fake_ob.commands() == ["sync-list-remote", "sync-list-local", "sync-setup", "sync-config"]
    calls = fake_ob.calls()
    setup = calls[2]["argv"]
    # The resolved id, never the name ob would look up in shared vaults too.
    assert setup[setup.index("--vault") + 1] == OWNED_ID
    assert setup[setup.index("--password") + 1] == "fake-e2ee-password"
    assert setup[setup.index("--device-name") + 1] == "anchor-railway"
    assert "--json" in setup
    config = calls[3]["argv"]
    assert config == [
        "sync-config",
        "--path", str(data_dir / "vault"),
        "--mode", "bidirectional",
        "--conflict-strategy", "merge",
        "--configs", "",
        "--json",
    ]
    assert not any("--file-types" in call["argv"] for call in calls)
    for directory in ("vault", "config", "tmp"):
        assert (data_dir / directory).is_dir()
    assert set(env) == {"PATH", "HOME", "XDG_CONFIG_HOME", "OBSIDIAN_AUTH_TOKEN"}


async def test_every_boot_runs_sync_config_and_a_linked_vault_skips_setup(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir, local=((OWNED_ID, str(data_dir / "vault")),))
    await _boot(boot_env, fake_ob)
    await _boot(boot_env, fake_ob)
    assert fake_ob.commands() == ["sync-list-remote", "sync-list-local", "sync-config"] * 2


async def test_ob_children_never_see_the_api_token(boot_env, fake_ob, data_dir) -> None:
    _ready(fake_ob, data_dir)
    await _boot(boot_env, fake_ob)
    for call in fake_ob.calls():
        assert "VAULT_API_TOKEN" not in call["env"]
        assert "OBSIDIAN_E2EE_PASSWORD" not in call["env"]
        assert "OBSIDIAN_AUTH_TOKEN" in call["env"]
