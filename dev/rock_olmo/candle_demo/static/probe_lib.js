// Prompt builders and scorers shared by the page and the node check (test_probe_lib.js).
// They mirror the Python probes: corpus_xml.stripped_record + fim_transform.fim_wrap (PSM)
// for prompts, probe_chains.extract / score and probe_scoring.score("bands") for verdicts.
(function (root) {
  const PRE = "<|fim_prefix|>", SUF = "<|fim_suffix|>", MID = "<|fim_middle|>";
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const digits = (v) => String(v).split("").join(" ");
  const psm = (pre, suf) => PRE + pre + SUF + suf + MID;

  // bands (strongest first) -> name, resolution field 1, digit-spaced values (§22j-§22l format)
  function bandsToName(bands) {
    const inner = bands.map((b, i) => (i === 0 ? `<top>${digits(b)}</top>` : `<next>${digits(b)}</next>`)).join("");
    return psm('<mineral species="', `">\n<raman resolution_cm1="1">${inner}</raman>\n</mineral>`);
  }
  // name -> formula (granular pair, plain text)
  function nameToFormula(name) {
    return psm(`<mineral species="${esc(name)}" formula="`, '">\n</mineral>');
  }

  function extract(gen, target) {
    const g = gen.replace(/^\s+/, "");
    if (target === "bands") return g.split("\n")[0].trim();
    const stop = target === "name" || target === "formula" ? '"' : "<";
    return g.split(stop)[0].split("\n")[0].trim();
  }
  const canon = (s) => s.replace(/\s+/g, "").replace(/·/g, ".").toLowerCase().replace(/\.+$/, "");
  const numbers = (text, limit) => (text.match(/\d+(?:\.\d+)?/g) || []).slice(0, limit).map(Number);

  // -> "HIT" | "NEAR" | "MISS"
  function score(target, gen, gold) {
    const a = extract(gen, target);
    if (target === "name") {
      const first = a.split(/[\n.,;("]/)[0].trim().toLowerCase();
      if (first === gold.name.toLowerCase()) return "HIT";
      return a.toLowerCase().includes(gold.name.toLowerCase()) ? "NEAR" : "MISS";
    }
    if (target === "formula") {
      const tok = a.trim().split(/[\s,;]/)[0].replace(/\.+$/, "");
      const f = canon(gold.formula);
      if (canon(tok) === f || canon(a.replace(/\.+$/, "")) === f) return "HIT";
      return canon(a).includes(f) ? "NEAR" : "MISS";
    }
    // bands: >= 2 of the 3 strongest reference bands within ±10 cm-1 of any of the first 12 numbers
    const nums = numbers(gen, 12);
    const hits = gold.bands.slice(0, 3).filter((b) => nums.some((x) => Math.abs(b - x) <= 10)).length;
    return hits >= 2 ? "HIT" : "MISS";
  }

  // The band positions a fill wrote, for display: <top>1086</top><next>282</next> -> [1086, 282]
  function bandList(gen) {
    return [...extract(gen, "bands").matchAll(/<(?:top|next)>\s*([\d.]+)\s*</g)].map((m) => Number(m[1]));
  }

  const api = { bandsToName, nameToFormula, extract, score, bandList, digits };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ProbeLib = api;
})(this);
