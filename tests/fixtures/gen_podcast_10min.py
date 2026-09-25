"""Generate tests/fixtures/podcast_10min.json — a deterministic ~10-minute
synthetic podcast transcript with 4 clearly separated topics, so the offline
TextTiling segmenter has real boundaries to find.

Topics: founder routines (0-150s), EV batteries (150-300s),
sourdough baking (300-450s), sleep science (450-600s).
Run:  cd ~/workspace/clipforge2 && .venv/bin/python tests/fixtures/gen_podcast_10min.py
"""

import json
import os
import random

random.seed(20260925)

TOPICS = [
    ("founder morning routines", [
        "Every successful founder protects their morning routine fiercely.",
        "The classic founder morning routine starts before sunrise.",
        "Journaling is the founder habit nobody skips in the morning routine.",
        "Cold showers sound miserable, but founders swear by them in the morning routine.",
        "A founder who walks without headphones gets better morning routine ideas.",
        "Protein breakfasts keep every founder sharp through the morning routine.",
        "The morning routine of a founder lists one task, not twenty.",
        "Meditation helps founders, though silence beats any morning routine app.",
        "Founders who train daily rarely burn out, morning routine intact.",
        "Reading twenty pages is the founder morning routine for expertise.",
        "A founder keeps the phone on airplane mode until the morning routine ends.",
        "Weekly reviews make the founder morning routine a learning machine.",
        "Saying no to meetings protects the founder morning routine.",
        "A founder's consistent wake time anchors the whole morning routine.",
        "Founders batch admin work so the morning routine stays sacred.",
        "The two-minute rule keeps the founder morning routine procrastination-free.",
        "Sunlight within thirty minutes resets the founder morning routine clock.",
        "Handwritten goals make the founder morning routine harder to skip.",
        "Accountability partners keep the founder morning routine honest.",
        "Ninety-minute deep work sprints define the founder morning routine.",
        "Inbox triage, not inbox zero, fits the founder morning routine.",
        "Walking meetings beat standing desks in any founder morning routine.",
        "Founders track energy, not hours, inside the morning routine.",
        "A shutdown ritual protects the founder's next morning routine.",
        "Gratitude journaling quietly improves the founder morning routine.",
        "Time blocking beats willpower in every founder morning routine.",
        "The best founders defend morning routine time like investor money.",
        "Batching context switches saves the founder morning routine two hours.",
        "Morning pages dump anxiety out of the founder morning routine.",
        "Reviewing wins builds momentum for the founder morning routine.",
    ]),
    ("electric vehicle batteries", [
        "Lithium iron phosphate batteries now dominate affordable electric cars.",
        "Electric car battery density has tripled in a decade.",
        "Solid-state batteries promise safer electric cars with faster charging.",
        "The battery cathode decides every electric car's range.",
        "Nickel-rich batteries give electric cars range but age faster.",
        "Modern electric car batteries outlast three hundred thousand kilometers.",
        "Thermal management matters more than raw battery size in electric cars.",
        "Battery swapping refills an electric car faster than chargers.",
        "Second-life electric car batteries store solar power for neighborhoods.",
        "Recycling recovers ninety-five percent of every EV battery.",
        "Sodium-ion batteries could make city electric cars far cheaper.",
        "Electric car batteries taper charging speed after eighty percent.",
        "Preconditioning the battery before arrival saves electric car drivers minutes.",
        "Bidirectional charging turns the electric car battery into a grid asset.",
        "Electric car battery warranties now stretch past eight years.",
        "Cold weather temporarily shrinks every electric car battery's range.",
        "Cell-to-pack designs make electric car batteries structural.",
        "Silicon-rich anodes keep improving electric car batteries yearly.",
        "Fast charging heats the battery and ages electric cars early.",
        "Battery passports will track each electric car battery's life.",
        "Lithium prices crashed after miners oversupplied battery makers.",
        "Electric trucks need megawatt charging for their giant batteries.",
        "Battery swapping suits delivery fleets better than electric car chargers.",
        "Calendar aging wears the battery even when the electric car sits parked.",
        "New electrolytes let batteries run at higher voltages safely.",
        "Range anxiety fades once electric car drivers trust battery networks.",
        "Home charging covers ninety percent of electric car battery needs.",
        "Battery fires are rare but brutal in electric cars.",
        "Modular batteries let electric car owners upgrade capacity later.",
        "The cheapest electric car battery ever just rolled off the line.",
    ]),
    ("sourdough baking", [
        "A lively starter doubles reliably within six hours of feeding.",
        "Whole wheat flour wakes a sluggish starter faster than white.",
        "Autolyse builds dough strength before the salt goes in.",
        "Stretch and folds replace kneading for wet dough with an active starter.",
        "Bulk fermentation ends when the dough grows by half, starter willing.",
        "The poke test never lies about dough readiness from a ripe starter.",
        "Cold retard deepens dough flavor from a mature starter overnight.",
        "Scoring the dough at an angle creates that proud ear.",
        "Steam in the first twenty minutes decides the dough's crust.",
        "A Dutch oven traps moisture the home dough needs.",
        "Underbaked dough gums up because the starter's starch never set.",
        "Dark crusts taste better; pale dough tastes of raw starter flour.",
        "Feeding ratios control how sour the starter makes the dough.",
        "Discard starter makes the best pancakes from leftover dough.",
        "High-hydration dough needs confident hands and a strong starter.",
        "Shaping builds dough tension for a tall oven spring.",
        "Rice flour keeps the dough from sticking, starter or not.",
        "Slash the dough too deep and it spreads instead of rising.",
        "The dough is done at ninety-eight degrees inside.",
        "Wait two hours before slicing the dough or the crumb gums.",
        "Starters survive weeks of neglect when the dough rests in the fridge.",
        "Reviving a flat starter takes three strong dough feedings.",
        "Chlorinated water stalls the starter and the dough both.",
        "Salt tightens dough gluten but slows a young starter.",
        "Lamination strengthens dough without extra kneading of the starter build.",
        "Coil folds suit very wet dough from a lively starter.",
        "A warm spot speeds a sluggish starter's dough rise.",
        "Weigh the dough ingredients; cups lie about starter flour.",
        "Old starters give dough wonderfully complex acidity.",
        "Your tenth dough will embarrass your first starter loaf.",
    ]),
    ("sleep science", [
        "Deep sleep washes metabolic waste out of the brain.",
        "REM sleep consolidates emotional memories in the brain overnight.",
        "Caffeine's six-hour half-life ruins the brain's sleep pressure.",
        "Blue light delays the brain's melatonin and sleep onset.",
        "Twenty-minute naps refresh the brain without wrecking night sleep.",
        "Sleep deprivation impairs the brain like alcohol does.",
        "Consistent bedtimes protect the brain better than catch-up sleep.",
        "Cool rooms near eighteen degrees deepen brain sleep.",
        "Exercise improves sleep quality, and the brain, unless too late.",
        "Alcohol fragments the brain's sleep architecture badly.",
        "Dreams let the sleeping brain rehearse threats safely.",
        "Sleep spindles shield the sleeping brain from noise.",
        "Teenage brains are wired to delay sleep naturally.",
        "Shift work measurably harms the sleep-deprived brain.",
        "The brain consolidates memory in slow-wave sleep.",
        "Sleep apnea hides behind snoring and starves the brain.",
        "Nasal breathing matters more for the sleeping brain than mouth tape.",
        "White noise won't deepen sleep, but the brain likes the masking.",
        "Sleep trackers guess; only lab gear reads the brain truly.",
        "Insomnia therapy beats pills for the sleepless brain long term.",
        "A wind-down routine tells the brain sleep is coming.",
        "Fiction before bed helps the brain fall asleep faster.",
        "Heavy late meals disrupt the brain's sleep cycles.",
        "Magnesium helps sleep only when the brain lacks it.",
        "Morning sunlight anchors the brain's sleep schedule.",
        "Weekend catch-up can't repay the brain's sleep debt.",
        "Reality checks train the brain for lucid sleep.",
        "Growing brains need far more sleep than adults.",
        "Napping cultures show healthier brains and hearts.",
        "The brain never truly sleeps; it just changes shifts.",
    ]),
]

SEGMENT_SECONDS = 150.0  # each topic spans 150s -> 600s total


def main() -> None:
    items: list[dict] = []
    t = 0.0
    rng = random.Random(20260925)
    for _topic_name, sentences in TOPICS:
        for s in sentences:
            dur = 4.2 + rng.random() * 1.3
            items.append({"start": round(t, 2), "end": round(t + dur, 2), "text": s})
            t += dur + 0.35 + rng.random() * 0.4
    # Normalize so the whole thing spans ~600s
    scale = 600.0 / t
    for it in items:
        it["start"] = round(it["start"] * scale, 2)
        it["end"] = round(it["end"] * scale, 2)
    out = {
        "_meta": {
            "generator": "tests/fixtures/gen_podcast_10min.py (seed 20260925)",
            "description": "Synthetic 10-min podcast, 4 topics x 30 sentences",
            "sentences": len(items),
            "duration_s": items[-1]["end"],
        },
        "transcript": items,
    }
    path = os.path.join(os.path.dirname(__file__), "podcast_10min.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {path}: {len(items)} sentences, {items[-1]['end']:.1f}s")


if __name__ == "__main__":
    main()
