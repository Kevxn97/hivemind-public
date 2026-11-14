"""Local incoming-knowledge switch shared by handoffs and source imports.

Call require_running inside the canonical write lock so pausing and applying
incoming knowledge have one ordering. Explicit human edits remain available.
"""
import json
from pathlib import Path

if __package__:
    from . import hive
    from .agent_contract import AgentError
else:
    import hive
    from agent_contract import AgentError


def status(root):
    path = Path(root) / '.state' / 'automation.json'
    state = json.loads(path.read_text()) if path.exists() else {'version': 1, 'paused': False}
    if not isinstance(state, dict) or state.get('version') != 1 or type(state.get('paused')) is not bool:
        raise ValueError('Invalid automation state; inspect .state/automation.json')
    revision = hive.digest(json.dumps(state, sort_keys=True, ensure_ascii=False))
    return {**state, 'revision': revision}


def require_running(root):
    if status(root)['paused']:
        raise AgentError('INVALID_STATE', 'Incoming knowledge processing is paused. Use hive memory resume after review.')


def set_paused(root, paused, *, actor, reason, expected_revision):
    if not actor.strip() or not reason.strip():
        raise ValueError('Actor and reason are required')
    with hive.locked(root):
        hive.require_clean(root)
        current = status(root)
        if current['revision'] != expected_revision:
            raise AgentError('REVISION_CONFLICT', 'Automation state changed; preview again')
        state = {'version': 1, 'paused': bool(paused), 'actor': actor, 'reason': reason,
                 'at': hive.now(), 'previous_revision': current['revision']}
        hive.atomic(Path(root) / '.state' / 'automation.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        return status(root)


def pause(root, *, actor, reason, expected_revision):
    return set_paused(root, True, actor=actor, reason=reason, expected_revision=expected_revision)


def resume(root, *, actor, reason, expected_revision):
    return set_paused(root, False, actor=actor, reason=reason, expected_revision=expected_revision)
