// node test_probe_lib.js — the page's prompt builders and scorers against the Python probes.
// Every canned prompt the page can rebuild must be byte-identical, and every stored verdict
// (bf16 reference and any recorded quantized run) must score the same in JS.
const fs = require("fs");
const lib = require("./static/probe_lib.js");
const doc = JSON.parse(fs.readFileSync(__dirname + "/static/probes.json", "utf8"));
const gold = Object.fromEntries(doc.species.map((s) => [s.name, s]));
let n = 0, bad = 0;
const fail = (msg) => { bad++; console.log("FAIL", msg); };
for (const p of doc.probes) {
  const g = gold[p.species];
  if (p.pair === "bands>name") { n++; if (lib.bandsToName(g.bands) !== p.prompt) fail(`${p.id} prompt`); }
  if (p.pair === "name>formula") { n++; if (lib.nameToFormula(g.name) !== p.prompt) fail(`${p.id} prompt`); }
  for (const [src, runs] of [["reference", p.reference], ["recorded", p.recorded || {}]]) {
    for (const [model, r] of Object.entries(runs)) {
      n++;
      const s = lib.score(p.target, r.text, g);
      if (s !== r.status) fail(`${p.id} ${src}/${model}: js ${s} vs py ${r.status} on ${JSON.stringify(r.text.slice(0, 60))}`);
    }
  }
}
console.log(`${n - bad}/${n} checks agree with the Python probes`);
process.exit(bad ? 1 : 0);
