"""Qualification maths for +qualify. Pure Python: no Discord, no images, no database.

A group is simulated many times: every remaining match is won by one team (chance from the two teams'
records), points are added, and the table is cut at the qualifying places.

Tie-breaker (the tournament rule): if teams are level on points at the cut and there are fewer places than
teams, those teams play a round-robin among themselves - every team plays every other once - and the most
wins goes through. If teams are still level at the cut after that, they play another round.
Example: 5 teams, top 4 qualify, one team on 14 points and four on 10 -> the four play 6 matches for 3 places.

Everything here works on team indexes; `Group` turns names in and out.
"""
import random
from collections import Counter
from itertools import combinations


def win_chance(a, b, prior=1.0):
    """Chance that team a beats team b. a, b = (wins, losses). log5 on smoothed win rates."""
    ra = (a[0] + prior) / (a[0] + a[1] + 2 * prior)
    rb = (b[0] + prior) / (b[0] + b[1] + 2 * prior)
    den = ra * (1 - rb) + rb * (1 - ra)
    return 0.5 if den <= 0 else ra * (1 - rb) / den


class Group:
    """Points table + remaining fixtures of one group."""

    def __init__(self, points, fixtures, prob):
        """points {team: points}; fixtures [(team a, team b, chance a wins)]; prob(a, b) -> chance a beats b."""
        self.names = list(points)
        self.ix = {n: i for i, n in enumerate(self.names)}
        self.n = len(self.names)
        self.pts = [points[n] for n in self.names]
        self.fx = [(self.ix[a], self.ix[b], p) for a, b, p in fixtures]
        self.pair = [[prob(a, b) if a != b else 0.5 for b in self.names] for a in self.names]

    # ---- one simulated season
    def play(self, rng, forced=None):
        pts = list(self.pts)
        rand = rng.random
        for i, (a, b, p) in enumerate(self.fx):
            if forced is not None and i in forced:
                win_a = forced[i] == a
            else:
                win_a = rand() < p
            if win_a:
                pts[a] += 2
            else:
                pts[b] += 2
        return pts

    def tiebreak(self, tied, need, rng):
        """Round-robin among the level teams for `need` places; returns the set that goes through."""
        through, tied = set(), list(tied)
        for _ in range(8):
            wins = dict.fromkeys(tied, 0)
            for a, b in combinations(tied, 2):
                wins[a if rng.random() < self.pair[a][b] else b] += 1
            order = sorted(tied, key=lambda t: (-wins[t], rng.random()))
            cut = wins[order[need - 1]]
            above = [t for t in tied if wins[t] > cut]
            level = [t for t in tied if wins[t] == cut]
            through.update(above)
            need -= len(above)
            if len(level) <= need:
                return through | set(level)
            tied = level
        rng.shuffle(tied)                                  # eight dead heats in a row: draw lots
        return through | set(tied[:need])

    def cut(self, pts, places, rng):
        """(teams through, teams through outright, level teams or None) for the top `places` of the table."""
        if places >= self.n:
            everyone = set(range(self.n))
            return everyone, everyone, None
        edge = sorted(pts, reverse=True)[places - 1]
        safe = {t for t in range(self.n) if pts[t] > edge}
        level = [t for t in range(self.n) if pts[t] == edge]
        room = places - len(safe)
        if len(level) <= room:
            through = safe | set(level)
            return through, through, None
        return safe | self.tiebreak(level, room, rng), safe, level


def simulate(group, spots, sims, rng, bonus=0):
    """Whole-group numbers: through / outright / level-at-cut / tie-break wins per team, bonus places, and
    which tie-break line-ups happen and how often."""
    n = group.n
    through, outright, tied, won = [0] * n, [0] * n, [0] * n, [0] * n
    b_through, b_outright = [0] * n, [0] * n
    scenes = {}
    for _ in range(sims):
        pts = group.play(rng)
        got, sure, level = group.cut(pts, spots, rng)
        for t in got:
            through[t] += 1
        for t in sure:
            outright[t] += 1
        if level:
            key = (tuple(sorted(level)), spots - sum(1 for t in range(n) if pts[t] > sorted(pts, reverse=True)[spots - 1]))
            scene = scenes.setdefault(key, [0, dict.fromkeys(level, 0)])
            scene[0] += 1
            for t in level:
                tied[t] += 1
                if t in got:
                    won[t] += 1
                    scene[1][t] += 1
        if bonus:
            bgot, bsure, _ = group.cut(pts, bonus, rng)
            for t in bgot:
                b_through[t] += 1
            for t in bsure:
                b_outright[t] += 1
    return {
        "through": [c / sims for c in through], "outright": [c / sims for c in outright],
        "tied": [c / sims for c in tied], "tie_win": [(won[t] / tied[t] if tied[t] else None) for t in range(n)],
        "bonus": [c / sims for c in b_through], "bonus_outright": [c / sims for c in b_outright],
        "scenes": [{"teams": [group.names[t] for t in teams], "room": room, "p": c / sims,
                    "win": {group.names[t]: w / c for t, w in wins.items()}}
                   for (teams, room), (c, wins) in sorted(scenes.items(), key=lambda kv: -kv[1][0])],
    }


def team_stats(group, team, spots, sims, rng, own_wins=None, forced=None, bonus=0):
    """What happens to one team when exactly `own_wins` of its matches are won, or given results are forced."""
    t = group.ix[team]
    own = [i for i, f in enumerate(group.fx) if t in (f[0], f[1])]
    through = outright = tied = won = b_through = 0
    for _ in range(sims):
        f = dict(forced) if forced else {}
        if own_wins is not None:
            winners = set(rng.sample(own, own_wins))
            for i in own:
                a, b, _p = group.fx[i]
                f[i] = t if i in winners else (b if a == t else a)
        pts = group.play(rng, f)
        got, sure, level = group.cut(pts, spots, rng)
        through += t in got
        outright += t in sure
        if level and t in level:
            tied += 1
            won += t in got
        if bonus:
            b_through += t in group.cut(pts, bonus, rng)[0]
    return {"through": through / sims, "outright": outright / sims, "tied": tied / sims,
            "tie_win": (won / tied if tied else None), "bonus": b_through / sims if bonus else None}


def analyse(points, fixtures, prob, team, spots, bonus, seed, sims=4000, scen_sims=1000, else_sims=600):
    """Everything +qualify shows. Returns plain dicts keyed by team name."""
    group = Group(points, fixtures, prob)
    rng = random.Random(seed)
    names = group.names
    base = simulate(group, spots, sims, rng, bonus)
    t = group.ix[team]
    own = [i for i, f in enumerate(group.fx) if t in (f[0], f[1])]
    k = len(own)
    scen = [(w, team_stats(group, team, spots, scen_sims, rng, own_wins=w, bonus=bonus)) for w in range(k, -1, -1)]
    swings = []
    for i in own:
        a, b, p = group.fx[i]
        opp = b if a == t else a
        swings.append({
            "opp": names[opp], "p": p if a == t else 1 - p, "idx": i,
            "win": team_stats(group, team, spots, scen_sims, rng, forced={i: t}, bonus=bonus),
            "lose": team_stats(group, team, spots, scen_sims, rng, forced={i: opp}, bonus=bonus),
        })
    elsewhere = []
    for i, (a, b, p) in enumerate(group.fx):
        if t in (a, b):
            continue
        ra = team_stats(group, team, spots, else_sims, rng, forced={i: a})["through"]
        rb = team_stats(group, team, spots, else_sims, rng, forced={i: b})["through"]
        good, bad = (a, ra) if ra >= rb else (b, rb)
        other = b if good == a else a
        elsewhere.append({"winner": names[good], "loser": names[other], "swing": abs(ra - rb),
                          "if_win": max(ra, rb), "if_lose": min(ra, rb)})
    elsewhere.sort(key=lambda e: -e["swing"])
    idx = {n: i for i, n in enumerate(names)}
    per_team = {n: {key: base[key][idx[n]] for key in ("through", "outright", "tied", "tie_win", "bonus", "bonus_outright")}
                for n in names}
    mine_pts = group.pts[t]
    return {"teams": per_team, "scen": scen, "swings": swings, "elsewhere": elsewhere, "left": k,
            "scenes": [s for s in base["scenes"] if team in s["teams"]][:4],
            "all_scenes": base["scenes"][:4], "best": mine_pts + 2 * k, "worst": mine_pts,
            "pair": {(a, b): group.pair[idx[a]][idx[b]] for a in names for b in names if a != b}}
