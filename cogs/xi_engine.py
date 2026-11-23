"""Best playing XI from real stats. Pure logic: no Discord, no images, no database.

Every player is rated 0-100 for batting, bowling and fielding by ranking him against every
other player in the server (percentiles), so a rating means the same thing for every team and
two XIs can be compared fairly. Strike rate hardly matters (5% of batting): what wins matches
here is runs, averages, wickets and bowling averages.

    batting    average 35%, total runs 25%, runs per match 25%, strike rate 5%, +10% experience
    bowling    wickets 25%, wickets per match 20%, bowling average 25%, bowling strike rate 15%,
               economy 15%
    fielding   catches 40%, catches per match 20%, catch efficiency 40%
    form       the last 5 matches against everyone else's last 5 (fantasy points per match); it also
               moves the batting and bowling ratings by a quarter of the gap
    value      what the selection ranks on:
               40% his better skill + 15% his other skill (all-rounders gain here)
               + 15% fantasy points + 10% fielding + 8% matches played + 12% recent form

Ratings are pulled towards a cautious 40 until a player has a few matches behind him (3 matches
= half way), so one lucky game never makes a star.

Roles: Bowler / All-Rounder / Batsman (Opener or Middle Order, by his place in the batting order).
Selection: best by value, then swaps until the side has enough genuine batters and bowlers; the
captain plays.
"""
from bisect import bisect_left, bisect_right

PRIOR = 40.0
RECENT = 5                    # matches that count as "recent form"
MIN_BALLS_TO_BOWL = 6          # a player who has bowled less than one over is not treated as a bowler


# --------------------------------------------------------------------------- profiles

def profile(uid, rows, caught, dropped, totals_fn, fantasy_fn):
    """One player's numbers from his match rows (dicts with runs, balls_faced, ...)."""
    t = totals_fn(rows)
    matches = len({r["mid"] for r in rows})
    batted = [r for r in rows if r["balls_faced"] > 0]
    wickets = int(t["wickets"])
    chances = caught + dropped
    ordered = sorted(rows, key=lambda r: r["mid"])            # match ids grow with time
    recent = ordered[-RECENT:]
    rt = totals_fn(recent)
    r_batted = [r for r in recent if r["balls_faced"] > 0]
    trend = [float(fantasy_fn([r])) for r in recent]          # fantasy points of each of the last matches
    return {
        "r_matches": len(recent), "trend": trend,
        "r_rpm": rt["runs"] / len(recent) if r_batted else None,
        "r_avg": float(rt["bat_avg"]) if r_batted else None,
        "r_wpm": rt["wickets"] / len(recent) if rt["balls_bowled"] >= MIN_BALLS_TO_BOWL else None,
        "r_econ": float(rt["economy"]) if rt["balls_bowled"] >= MIN_BALLS_TO_BOWL else None,
        "r_fpm": sum(trend) / len(trend) if trend else None,
        "uid": uid, "matches": matches,
        "bat_inn": len(batted), "runs": int(t["runs"]), "balls_faced": int(t["balls_faced"]),
        "bat_avg": float(t["bat_avg"]) if batted else None,
        "strike_rate": float(t["strike_rate"]) if t["balls_faced"] else None,
        "wickets": wickets, "balls_bowled": int(t["balls_bowled"]), "conceded": int(t["runs_conceded"]),
        "economy": float(t["economy"]) if t["balls_bowled"] >= MIN_BALLS_TO_BOWL else None,
        "bowl_avg": float(t["bowl_avg"]) if wickets else None,
        "bowl_sr": float(t["bowl_sr"]) if wickets else None,
        "catches": int(caught), "drops": int(dropped),
        "catch_eff": caught / chances * 100 if chances else None,
        "fantasy": float(fantasy_fn(rows)) + caught * 5 - dropped * 10 if (rows or chances) else 0.0,
    }


def _metrics(p):
    m = max(p["matches"], 1)
    return {
        "bat_avg": p["bat_avg"], "runs": p["runs"] if p["bat_inn"] else None,
        "rpm": p["runs"] / m if p["bat_inn"] else None, "sr": p["strike_rate"],
        "wickets": p["wickets"] if p["balls_bowled"] >= MIN_BALLS_TO_BOWL else None,
        "wpm": p["wickets"] / m if p["balls_bowled"] >= MIN_BALLS_TO_BOWL else None,
        "bowl_avg": p["bowl_avg"], "bowl_sr": p["bowl_sr"], "economy": p["economy"],
        "catches": p["catches"], "cpm": p["catches"] / m, "eff": p["catch_eff"],
        "fantasy": p["fantasy"], "matches": p["matches"],
        "r_rpm": p.get("r_rpm"), "r_avg": p.get("r_avg"), "r_wpm": p.get("r_wpm"),
        "r_econ": p.get("r_econ"), "r_fpm": p.get("r_fpm"),
    }


class Pool:
    """Sorted value lists of every player in the server, to turn a number into a percentile."""

    def __init__(self, profiles):
        self.lists = {}
        for p in profiles:
            if not p["matches"]:
                continue
            for key, value in _metrics(p).items():
                if value is not None:
                    self.lists.setdefault(key, []).append(value)
        for values in self.lists.values():
            values.sort()

    def pct(self, key, value, higher=True):
        """0-100: how many players this value beats (ties count half). None for no value."""
        if value is None:
            return None
        values = self.lists.get(key)
        if not values:
            return 50.0
        below, upto = bisect_left(values, value), bisect_right(values, value)
        score = (below + (upto - below) / 2) / len(values) * 100
        return score if higher else 100 - score


def _mean(pairs):
    """Weighted mean of (weight, value) skipping missing values; None if nothing is left."""
    pairs = [(w, v) for w, v in pairs if v is not None]
    total = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / total if total else None


def rate(p, pool):
    """Fill in bat / bowl / field / value (0-100) and the role for one profile."""
    m = _metrics(p)
    trust = p["matches"] / (p["matches"] + 3)          # 0 matches = 0, 3 matches = 0.5, 12 = 0.8

    def shrink(raw):
        return PRIOR * (1 - trust) + raw * trust

    experience = pool.pct("matches", p["matches"]) if p["matches"] else 0
    bat_raw = _mean([
        (0.35, pool.pct("bat_avg", m["bat_avg"])), (0.25, pool.pct("runs", m["runs"])),
        (0.25, pool.pct("rpm", m["rpm"])), (0.05, pool.pct("sr", m["sr"])),
    ])
    bat = shrink(bat_raw * 0.9 + experience * 0.1) if bat_raw is not None else 15.0

    bowls = p["balls_bowled"] >= MIN_BALLS_TO_BOWL
    bowl = None
    if bowls:
        # no wickets yet = no average / strike rate: those parts count as the worst (0)
        raw = _mean([
            (0.25, pool.pct("wickets", m["wickets"])), (0.20, pool.pct("wpm", m["wpm"])),
            (0.25, pool.pct("bowl_avg", m["bowl_avg"], higher=False) if m["bowl_avg"] is not None else 0.0),
            (0.15, pool.pct("bowl_sr", m["bowl_sr"], higher=False) if m["bowl_sr"] is not None else 0.0),
            (0.15, pool.pct("economy", m["economy"], higher=False)),
        ])
        bowl = shrink(raw)

    field_raw = _mean([
        (0.40, pool.pct("catches", m["catches"])), (0.20, pool.pct("cpm", m["cpm"])),
        (0.40, pool.pct("eff", m["eff"]) if m["eff"] is not None else None),
    ])
    field = shrink(field_raw) if p["matches"] else 20.0

    fantasy = shrink(pool.pct("fantasy", m["fantasy"])) if p["matches"] else 20.0

    # recent form: how the last 5 matches compare with everybody else's last 5. It nudges the batting
    # and bowling ratings (25% of the gap) and is its own rating, which counts in the selection.
    rtrust = p["r_matches"] / (p["r_matches"] + 2)
    form_bat = _mean([(0.5, pool.pct("r_rpm", m["r_rpm"])), (0.5, pool.pct("r_avg", m["r_avg"]))])
    form_bowl = _mean([(0.5, pool.pct("r_wpm", m["r_wpm"])),
                       (0.5, pool.pct("r_econ", m["r_econ"], higher=False))])
    if form_bat is not None:
        bat += 0.25 * rtrust * (form_bat - bat)
    if bowl is not None and form_bowl is not None:
        bowl += 0.25 * rtrust * (form_bowl - bowl)
    form = PRIOR * (1 - rtrust) + (pool.pct("r_fpm", m["r_fpm"]) or 0) * rtrust if p["r_matches"] else 20.0

    strong, weak = (bat, bowl if bowl is not None else 0.0) if bat >= (bowl or 0) else (bowl, bat)
    value = (0.40 * strong + 0.15 * weak + 0.15 * fantasy + 0.10 * field
             + 0.08 * shrink(experience) + 0.12 * form)

    if bowl is None:
        role = "Batsman"
    elif bat >= 50 and bowl >= 50:
        role = "All-Rounder"
    elif bowl > bat:
        role = "Bowler"
    else:
        role = "Batsman"

    # how he plays: a batter with a high strike rate attacks, a low one anchors the innings;
    # a bowler with a low bowling strike rate takes wickets, a low economy holds runs back
    sr_pct = pool.pct("sr", m["sr"]) if m["sr"] is not None else 50.0
    wk_pct = pool.pct("bowl_sr", m["bowl_sr"], higher=False) if m["bowl_sr"] is not None else 30.0
    eco_pct = pool.pct("economy", m["economy"], higher=False) if m["economy"] is not None else 50.0
    style = "Aggressor" if sr_pct >= 58 else ("Anchor" if sr_pct <= 42 else "Balanced")
    bstyle = "Strike bowler" if wk_pct - eco_pct >= 12 else ("Economical" if eco_pct - wk_pct >= 12 else "Balanced")
    p.update(sr_pct=round(sr_pct, 1), wk_pct=round(wk_pct, 1), eco_pct=round(eco_pct, 1),
             style=style, bstyle=bstyle, form=round(form, 1), form_delta=round(form - fantasy, 1))
    p.update(bat=round(bat, 1), bowl=None if bowl is None else round(bowl, 1), field=round(field, 1),
             fantasy_rating=round(fantasy, 1), value=round(value, 1), role=role)
    return p


# --------------------------------------------------------------------------- selection

def _can_bat(p):
    return p["role"] in ("Batsman", "All-Rounder")


def _can_bowl(p):
    return p["role"] in ("Bowler", "All-Rounder")


def build(profiles, pool, size=11, captain=None, vice=None):
    """Return {"xi": [...in batting order], "subs": [...], "bench": [...], "size", "strength"}.

    Each entry is the profile dict plus: role, label ("Opener", "Middle Order", "All-Rounder",
    "Bowler"), order (batting position, 1-based, XI only), tag ("C" / "VC" / None), cover (subs).
    """
    people = [rate(dict(p), pool) for p in profiles]
    size = max(1, min(size, len(people)))
    ranked = sorted(people, key=lambda p: (-p["value"], -p["matches"], p["uid"]))
    chosen = ranked[:size]
    rest = [p for p in ranked if p not in chosen]

    if captain is not None and all(p["uid"] != captain for p in chosen):   # the captain plays
        leader = next((p for p in rest if p["uid"] == captain), None)
        if leader is not None and chosen:
            out = min((p for p in chosen if p["uid"] != vice), key=lambda p: p["value"], default=chosen[-1])
            chosen[chosen.index(out)] = leader
            rest = [p for p in rest if p is not leader] + [out]

    need = max(2, round(size * 0.4))
    for ok, other_ok in ((_can_bowl, _can_bat), (_can_bat, _can_bowl)):
        while sum(1 for p in chosen if ok(p)) < need:
            candidate = next((p for p in rest if ok(p)), None)
            if candidate is None:
                break
            keep_other = sum(1 for p in chosen if other_ok(p))
            victims = [p for p in chosen
                       if not ok(p) and p["uid"] != captain
                       and not (other_ok(p) and keep_other - 1 < need)]
            if not victims:
                break
            out = min(victims, key=lambda p: p["value"])
            chosen[chosen.index(out)] = candidate
            rest = [p for p in rest if p is not candidate] + [out]
    rest.sort(key=lambda p: (-p["value"], -p["matches"], p["uid"]))

    order = batting_order(chosen)
    for number, p in enumerate(order, 1):
        p["order"] = number
        if p["role"] == "All-Rounder":
            p["label"] = "All-Rounder"
        elif p["role"] == "Bowler":
            p["label"] = "Bowler"
        else:
            p["label"] = "Opener" if number <= 2 else "Middle Order"
        p["tag"] = "C" if p["uid"] == captain else ("VC" if p["uid"] == vice else None)
    attack = bowling_order(chosen)

    subs, bench = rest[:min(4, len(rest))], rest[min(4, len(rest)):]
    for p in rest:
        p["label"] = {"All-Rounder": "All-Rounder", "Bowler": "Bowler"}.get(p["role"], "Batsman")
        p["tag"] = "C" if p["uid"] == captain else ("VC" if p["uid"] == vice else None)
    for p in subs:   # which XI player he would replace: the weakest one with the same job
        same = [q for q in order if q["role"] == p["role"]] or order
        p["cover"] = min(same, key=lambda q: q["value"])["uid"] if same else None

    return {"xi": order, "subs": subs, "bench": bench, "size": size, "strength": strength(order),
            "bowl_order": attack}


def batting_order(chosen):
    """The XI in batting order, balanced so the innings has both a steady and an attacking hand.

    Openers: the pair with the best batting AND the biggest difference in strike rate (one who
    anchors, one who attacks). After that each next batter is the best one left who differs in
    style from the man before him, so low and high strike rates alternate instead of bunching.
    Bowlers who can't bat come last.
    """
    from itertools import combinations
    batters = [p for p in chosen if p["role"] != "Bowler"]
    tail = sorted((p for p in chosen if p["role"] == "Bowler"), key=lambda p: -p["bat"])
    if len(batters) < 2:
        return batters + tail
    top = sorted(batters, key=lambda p: -p["bat"])[:5]
    pair = max(combinations(top, 2),
               key=lambda ab: ab[0]["bat"] + ab[1]["bat"] + 0.35 * abs(ab[0]["sr_pct"] - ab[1]["sr_pct"]))
    pair = sorted(pair, key=lambda p: (p["sr_pct"], -p["bat"]))           # the anchor faces the first ball
    order, left = list(pair), [p for p in batters if p not in pair]
    while left:
        prev = order[-1]
        nxt = max(left, key=lambda p: p["bat"] + 0.25 * abs(p["sr_pct"] - prev["sr_pct"]))
        order.append(nxt)
        left.remove(nxt)
    return order + tail


def bowling_order(chosen):
    """The attack in the order it would be used: opening pair, middle overs, death.

    Opening: the best two bowlers who differ in kind (one takes wickets, one is economical).
    Death: the most economical of the rest. Everyone else bowls the middle overs.
    Each entry gets "spell" ("Opening" / "Middle" / "Death") and "bowl_pos" (1-based).
    """
    from itertools import combinations
    bowlers = sorted((p for p in chosen if p["bowl"] is not None), key=lambda p: -p["bowl"])[:6]
    if not bowlers:
        return []
    kind = lambda p: p["wk_pct"] - p["eco_pct"]                         # + takes wickets, - holds runs
    if len(bowlers) >= 2:
        opening = max(combinations(bowlers[:4], 2),
                      key=lambda ab: ab[0]["bowl"] + ab[1]["bowl"] + 0.3 * abs(kind(ab[0]) - kind(ab[1])))
        opening = sorted(opening, key=lambda p: -p["bowl"])
    else:
        opening = (bowlers[0],)
    rest = [p for p in bowlers if p not in opening]
    death = max(rest, key=lambda p: (p["eco_pct"], p["bowl"])) if len(rest) >= 2 else None
    middle = sorted((p for p in rest if p is not death), key=lambda p: -p["bowl"])
    plan = [(p, "Opening") for p in opening] + [(p, "Middle") for p in middle] + ([(death, "Death")] if death else [])
    out = []
    for number, (p, spell) in enumerate(plan, 1):
        p["spell"], p["bowl_pos"] = spell, number
        out.append(p)
    return out


def strength(xi):
    """Category strengths of an XI (0-100) for comparing two sides."""
    if not xi:
        return {"batting": 0.0, "bowling": 0.0, "allround": 0.0, "fielding": 0.0, "depth": 0.0, "overall": 0.0,
                "form": 0.0}
    bats = sorted((p["bat"] for p in xi), reverse=True)[:6]
    bowls = sorted((p["bowl"] for p in xi if p["bowl"] is not None), reverse=True)[:5]
    ars = [min(p["bat"], p["bowl"]) for p in xi if p["role"] == "All-Rounder"]
    depth = sorted((p["value"] for p in xi))
    out = {
        "batting": sum(bats) / len(bats),
        "bowling": sum(bowls) / len(bowls) if bowls else 0.0,
        "allround": sum(ars) / len(ars) if ars else 0.0,
        "fielding": sum(p["field"] for p in xi) / len(xi),
        # depth is how good the weakest half is: a side that only works with its stars rates low here
        "depth": sum(depth[: max(1, len(depth) // 2)]) / max(1, len(depth) // 2),
        "overall": sum(p["value"] for p in xi) / len(xi),
        "form": sum(p["form"] for p in xi) / len(xi),
    }
    return {k: round(v, 1) for k, v in out.items()}
