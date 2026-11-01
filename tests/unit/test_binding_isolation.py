"""A patched package global must not outlive the test that patched it (#139).

`patch("llm_light.warm_cache")` restores only the package attribute. A wrapped
call made while the patch is active runs `_sync_bindings()`, which copies the
mock into `llm.warm_cache` and `main.warm_cache`, and nothing put those back:
the next test that called `llm.warm_cache()` hit the leftover mock and sent
nothing, and passed or failed on test order.

The two tests below run in file order. The first leaks the way
TestHeartbeatWarmsCache did; the second fails if the leak reached it. The
autouse fixture in tests/conftest.py is what keeps them apart.
"""
from unittest.mock import Mock, patch

import llm_light
from llm_light import llm, main, state


def test_1_a_test_that_patches_a_package_global_and_syncs():
    with patch("llm_light.warm_cache", side_effect=lambda quiet=False: None), \
         patch("llm_light.stop_event", Mock(name="leaked-event")):
        llm_light._sync_bindings()  # what any wrapped call does while patched
        assert isinstance(llm.warm_cache, Mock)
        assert isinstance(state.stop_event, Mock)


def test_2_the_next_test_sees_the_real_bindings():
    assert not isinstance(llm.warm_cache, Mock)
    assert not isinstance(main.warm_cache, Mock)
    assert not isinstance(state.stop_event, Mock)


def test_every_synced_binding_is_listed_once():
    """_sync_bindings writes exactly the table the fixture restores."""
    pairs = [(mod.__name__, name) for mod, names in llm_light._SYNC_TARGETS for name in names]
    assert len(pairs) == len(set(pairs))
    assert all(hasattr(llm_light, name) for _, name in pairs)
