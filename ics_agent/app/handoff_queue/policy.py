"""无 IO 的排队规则及配置校验。"""
from datetime import datetime, timedelta, timezone
import math
import random
BEIJING = timezone(timedelta(hours=8))
DEFAULT_CONFIG = {'enabled': False, 'daily_tiers': [dict(start=s, min_count=a, max_count=b) for (s, a, b) in [(1, 0, 0), (5, 2, 2), (8, 4, 6), (13, 10, 30)]], 'weekly_tiers': [dict(start=s, min_count=a, max_count=b) for (s, a, b) in [(1, 0, 0), (30, 5, 5), (50, 12, 12), (80, 20, 20)]], 'step_min_s': 30, 'step_max_s': 50, 'estimate_per_person_s': 40}

class ConfigError(ValueError):

    def __init__(self, errors):
        super().__init__('排队配置不合法')
        self.errors = errors

def validate_config(config):
    errors = []

    def error(path, message):
        errors.append({'loc': ['config', *path], 'msg': message})
    if not isinstance(config, dict):
        raise ConfigError([{'loc': ['config'], 'msg': '必须为对象'}])
    for key in set(config) - set(DEFAULT_CONFIG):
        error([key], '未知配置项')
    if type(config.get('enabled')) is not bool:
        error(['enabled'], '必须为开关')
    for name in ('daily_tiers', 'weekly_tiers'):
        tiers = config.get(name)
        if not isinstance(tiers, list):
            error([name], '必须为档位数组')
            continue
        previous = 0
        for (index, tier) in enumerate(tiers):
            if not isinstance(tier, dict):
                error([name, index], '必须为档位对象')
                continue
            for key in set(tier) - {'start', 'min_count', 'max_count'}:
                error([name, index, key], '未知配置项')
            for (key, low, high) in [('start', 1, 2147483647), ('min_count', 0, 200), ('max_count', 0, 200)]:
                value = tier.get(key)
                if type(value) is not int or not low <= value <= high:
                    error([name, index, key], f'请输入 {low}–{high} 的整数')
            start = tier.get('start')
            if type(start) is int:
                if start <= previous:
                    error([name, index, 'start'], '起始次数必须严格递增且不重复')
                previous = start
            (a, b) = (tier.get('min_count'), tier.get('max_count'))
            if type(a) is int and type(b) is int and (a > b):
                error([name, index, 'max_count'], '人数上限不得小于下限')
    for key in ('step_min_s', 'step_max_s', 'estimate_per_person_s'):
        value = config.get(key)
        if type(value) is not int or not 5 <= value <= 600:
            error([key], '请输入 5–600 的整数')
    (a, b) = (config.get('step_min_s'), config.get('step_max_s'))
    if type(a) is int and type(b) is int and (a > b):
        error(['step_max_s'], '递减间隔上限不得小于下限')
    if errors:
        raise ConfigError(errors)
    return config

def week_anchor(now):
    local = now.astimezone(BEIJING)
    anchor = local.replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=(local.weekday() - 4) % 7)
    if anchor > local:
        anchor -= timedelta(days=7)
    return anchor.astimezone(timezone.utc)

def success_counts(row, now):
    if not row:
        return (0, 0)
    return (sum((now - timedelta(days=1) < t <= now for t in row['success_times'])), row['week_count'] if row['week_anchor'] == week_anchor(now) else 0)

def initial_count(config, daily, weekly, seed):
    rng = random.Random(seed)

    def draw(tiers, n):
        selected = None
        for tier in tiers:
            if tier['start'] <= n:
                selected = tier
        return rng.randint(selected['min_count'], selected['max_count']) if selected else 0
    return draw(config['daily_tiers'], daily + 1) + draw(config['weekly_tiers'], weekly + 1)

def remaining(state, now):
    if state.get('redeem_ready'):
        return 0
    elapsed = max(0, (now - state['entered_at']).total_seconds())
    rng = random.Random(state['seed'])
    total = 0
    for step in range(state['initial_count']):
        total += rng.uniform(state['step_min_s'], state['step_max_s'])
        if total > elapsed:
            return state['initial_count'] - step
    return 0

def estimate_minutes(count, seconds):
    return max(1, math.ceil(count * seconds / 60))
