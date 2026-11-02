"""Persistence acceptance for the owned annular sweep's captured model."""

from ansa_mcp_bridge.operations import part_aggregates


def complete_persistence(report, before, after):
    """Complete only after the existing native aggregate audit accepts both sides."""
    report['complete'] = False
    try:
        report['save_reopen_raw_exact'] = before['snapshot'] == after['snapshot']
        report['persistence'] = part_aggregates.compare_persistence(
            before['snapshot'], after['snapshot'],
            {int(key): value for key, value in before['aggregates'].items()},
            {int(key): value for key, value in after['aggregates'].items()},
        )
    except Exception as exc:
        report['persistence'] = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
        raise
    if report['persistence']['status'] != 'pass':
        raise RuntimeError('annular save/reopen persistence was not verified')
    report['complete'] = True
