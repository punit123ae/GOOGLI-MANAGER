"""Match prediction maths for +predict (no Discord, no images).

For every player of the two squads we estimate, from his own recorded matches, how likely he is to play
and what he usually does when he does (runs, wickets, catches, fantasy points). Numbers from few matches
are blended with the league average (a 2-match average is not trusted like a 12-match one). The match is
then played thousands of times at random and we count how often each player is the best of his team and
the best of the match in each category. The pick is the player who most often comes out on top.
"""
import math
import random

CATEGORIES = ("runs", "wickets", "catches", "fantasy")
PRIOR = 2.0            # matches' worth of league average blended into a player's own numbers
MIN_PLAY = 0.35        # nobody is treated as less likely to play than this (squads change)


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _gamma(rng, mean, shape):
    return rng.gammavariate(shape, mean / shape) if mean > 0 else 0.0


def league_means(everyone):
    """Average per-match numbers of players who actually bat / bowl / field, used as the blending target.

    everyone: list of player_input() dicts for the whole league."""
    def avg(values, default):
        values = list(values)
        return sum(values) / len(values) if values else default
    return {
        "runs": avg((p["runs_bat"] for p in everyone if p["bat_n"]), 12.0),
        "wickets": avg((p["wk_bowl"] for p in everyone if p["bowl_n"]), 0.8),
        "catches": avg((p["catch_pm"] for p in everyone if p["rows"]), 0.4),
        "fantasy": avg((p["fant_pm"] for p in everyone if p["rows"]), 30.0),
    }


def player_input(uid, rows, fantasies, caught, team_matches):
    """One player's raw facts. rows: his match rows for this team; fantasies: fantasy points of each row."""
    n = len(rows)
    bat = [r["runs"] for r in rows if r["balls_faced"] > 0]
    bowl = [r["wickets"] for r in rows if r["balls_bowled"] > 0]
    return {
        "uid": uid, "rows": n, "team_matches": max(team_matches, n, 1),
        "bat_n": len(bat), "runs_bat": sum(bat) / len(bat) if bat else 0.0,
        "bowl_n": len(bowl), "wk_bowl": sum(bowl) / len(bowl) if bowl else 0.0,
        "catch_pm": caught / n if n else 0.0,
        "fant_pm": sum(fantasies) / n if n else 0.0,
    }


def _blend(own, n, prior, k=PRIOR):
    return (own * n + prior * k) / (n + k)


def expectation(p, league):
    """The numbers the simulation draws from for one player."""
    n = p["rows"]
    play = min(1.0, max(MIN_PLAY, n / p["team_matches"])) if n else 0.0
    return {
        "uid": p["uid"], "play": play,
        "bat": p["bat_n"] / n if n else 0.0,              # share of his matches in which he batted
        "bowl": p["bowl_n"] / n if n else 0.0,
        "runs": _blend(p["runs_bat"], p["bat_n"], league["runs"]) if p["bat_n"] else 0.0,
        "wickets": _blend(p["wk_bowl"], p["bowl_n"], league["wickets"]) if p["bowl_n"] else 0.0,
        "catches": _blend(p["catch_pm"], n, league["catches"]) if n else 0.0,
        "fantasy": max(0.0, _blend(p["fant_pm"], n, league["fantasy"])) if n else 0.0,
    }


def simulate(squads, sims=4000, seed=1):
    """squads: [[expectation dicts of team A], [... of team B]].

    Returns {category: {"team": [[{uid, chance, exp}, ...] for each team, best first], "match": [...]}}
    where chance = share of simulated matches in which he was the top (ties share the credit) and
    exp = what he is expected to score in a match (own chance to play included)."""
    rng = random.Random(seed)
    flat = [(t, e) for t, squad in enumerate(squads) for e in squad]
    wins = {c: {"team": [dict() for _ in squads], "match": {}} for c in CATEGORIES}
    for _ in range(sims):
        draw = {c: [] for c in CATEGORIES}
        for t, e in flat:
            if rng.random() > e["play"]:
                for c in CATEGORIES:
                    draw[c].append((t, e["uid"], -1.0))
                continue
            runs = _gamma(rng, e["runs"], 1.1) if rng.random() < e["bat"] else 0.0
            wk = _poisson(rng, e["wickets"]) if rng.random() < e["bowl"] else 0
            catches = _poisson(rng, e["catches"])
            fantasy = _gamma(rng, e["fantasy"], 2.2)
            draw["runs"].append((t, e["uid"], round(runs)))
            draw["wickets"].append((t, e["uid"], wk))
            draw["catches"].append((t, e["uid"], catches))
            draw["fantasy"].append((t, e["uid"], fantasy))
        for c in CATEGORIES:
            rows = draw[c]
            for t in range(len(squads)):
                mine = [r for r in rows if r[0] == t]
                if not mine:
                    continue
                best = max(r[2] for r in mine)
                if best < 0:
                    continue
                tied = [r for r in mine if r[2] == best]
                for _t, uid, _v in tied:
                    wins[c]["team"][t][uid] = wins[c]["team"][t].get(uid, 0.0) + 1.0 / len(tied)
            best = max(r[2] for r in rows) if rows else -1
            if best >= 0:
                tied = [r for r in rows if r[2] == best]
                for _t, uid, _v in tied:
                    wins[c]["match"][uid] = wins[c]["match"].get(uid, 0.0) + 1.0 / len(tied)
    exp = {e["uid"]: e for _t, e in flat}

    out = {}
    for c_name in CATEGORIES:
        out[c_name] = {"team": [table_for(wins[c_name]["team"][t], exp, c_name, sims) for t in range(len(squads))],
                       "match": table_for(wins[c_name]["match"], exp, c_name, sims)}
    return out


def _expected(e, category):
    if category == "runs":
        return e["play"] * e["bat"] * e["runs"]
    if category == "wickets":
        return e["play"] * e["bowl"] * e["wickets"]
    if category == "catches":
        return e["play"] * e["catches"]
    return e["play"] * e["fantasy"]


def table_for(counts, exp, category, sims):
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"uid": uid, "chance": v / sims, "exp": _expected(exp[uid], category)} for uid, v in ranked]
