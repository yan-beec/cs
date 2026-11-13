"""Shared helpers of the quality statistics page (AI figures come from the metric registry)."""


def percentage(failed, total):
    return round(failed / total * 100, 2) if total else None
