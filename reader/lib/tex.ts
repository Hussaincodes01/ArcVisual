/**
 * TeX for people who do not read TeX.
 *
 * Three jobs, one small parser:
 *
 * - `toUnicode` turns notation into readable math made of ordinary characters —
 *   `\sqrt{d_k}` becomes `√dₖ`, `\nabla_\phi \mathcal{L}` becomes `∇ᵩℒ`. Used where
 *   KaTeX cannot go (SVG labels, page titles, the outline) and as the last-resort
 *   fallback, so a reader never sees `\frac{` or `_{` again.
 * - `toWords` reads an equation aloud in plain English — "S equals Q K transpose
 *   over the square root of d sub k" — for readers who know what the paper is about
 *   but not how its notation works.
 * - `splitMath` separates the `$…$` math in model-written text from the words
 *   around it, so claims and captions typeset their math instead of printing it.
 *
 * It is deliberately forgiving: unknown commands fall back to their names, and
 * nothing here throws. It is not a TeX engine — the goal is "readable", not exact.
 */

// --------------------------------------------------------------------------- //
// Parsing
// --------------------------------------------------------------------------- //

type Node =
  | { k: "chr"; v: string }
  | { k: "cmd"; name: string; args: Node[][] }
  | { k: "grp"; body: Node[] }
  | { k: "scr"; base: Node | null; sub: Node[] | null; sup: Node[] | null };

/** Commands and how many brace arguments they take. */
const ARITY: Record<string, number> = {
  frac: 2, dfrac: 2, tfrac: 2, cfrac: 2, binom: 2, sqrt: 1, text: 1, textrm: 1, textit: 1,
  textbf: 1, mathrm: 1, mathbf: 1, mathit: 1, mathsf: 1, mathtt: 1, mathcal: 1, mathbb: 1,
  mathfrak: 1, mathscr: 1, boldsymbol: 1, bm: 1, operatorname: 1, hat: 1, widehat: 1,
  tilde: 1, widetilde: 1, bar: 1, overline: 1, vec: 1, dot: 1, ddot: 1, underline: 1,
  mbox: 1, emph: 1, overbrace: 1, underbrace: 1, color: 1, textcolor: 2,
};

function tokenize(src: string): string[] {
  const out: string[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      const m = /^\\([A-Za-z]+|.)/.exec(src.slice(i));
      if (m) {
        out.push(m[0]);
        i += m[0].length;
        continue;
      }
    }
    out.push(c);
    i++;
  }
  return out;
}

const chrOfNode = (n: Node | undefined): string | null => (n && n.k === "chr" ? n.v : null);

function parse(tokens: string[]): Node[] {
  let pos = 0;

  const group = (): Node[] => {
    // A brace group, or a single atom when there is no brace.
    while (tokens[pos] === " ") pos++;
    if (tokens[pos] === "{") {
      pos++;
      const body = seq("}");
      pos++; // the closing brace
      return body;
    }
    const a = atom();
    return a ? [a] : [];
  };

  const atom = (): Node | null => {
    const t = tokens[pos];
    if (t === undefined) return null;
    if (t === "{") {
      return { k: "grp", body: group() };
    }
    pos++;
    if (t.startsWith("\\") && t.length > 1) {
      const name = t.slice(1);
      if (name === "sqrt" && tokens[pos] === "[") {
        // \sqrt[n]{x}: keep the index as a first argument.
        pos++;
        const idx = seq("]");
        pos++;
        return { k: "cmd", name: "nroot", args: [idx, group()] };
      }
      const n = ARITY[name] ?? 0;
      const args: Node[][] = [];
      for (let j = 0; j < n; j++) args.push(group());
      return { k: "cmd", name, args };
    }
    return { k: "chr", v: t };
  };

  const seq = (until: string): Node[] => {
    const nodes: Node[] = [];
    while (pos < tokens.length && tokens[pos] !== until) {
      const t = tokens[pos];
      if (t === "}" && until !== "}") {
        pos++;
        continue; // stray closer
      }
      if (t === "_" || t === "^") {
        pos++;
        const script = group();
        let prev = nodes[nodes.length - 1];
        if (prev && prev.k === "chr" && /^[0-9.]$/.test(prev.v)) {
          let num = "";
          while (nodes.length && chrOfNode(nodes[nodes.length - 1]) !== null && /^[0-9.]$/.test(chrOfNode(nodes[nodes.length - 1])!)) {
            num = chrOfNode(nodes.pop())! + num;
          }
          prev = { k: "chr", v: num };
          nodes.push(prev);
        }
        let target: Extract<Node, { k: "scr" }>;
        if (prev && prev.k === "scr" && !(t === "_" ? prev.sub : prev.sup)) {
          target = prev;
        } else {
          target = { k: "scr", base: nodes.pop() ?? null, sub: null, sup: null };
          nodes.push(target);
        }
        if (t === "_") target.sub = script;
        else target.sup = script;
        continue;
      }
      const a = atom();
      if (a) nodes.push(a);
    }
    return nodes;
  };

  return seq("\u0000");
}

/** Expand the paper's own macros (with #1…#9 arguments) before parsing. */
export function expandMacros(src: string, macros: Record<string, string> = {}): string {
  if (!Object.keys(macros).length) return src;
  let out = src;
  for (let pass = 0; pass < 8; pass++) {
    let changed = false;
    let res = "";
    let i = 0;
    while (i < out.length) {
      if (out[i] !== "\\") {
        res += out[i++];
        continue;
      }
      const m = /^\\[A-Za-z]+/.exec(out.slice(i));
      if (!m) {
        res += out.slice(i, i + 2); // an escaped character such as \{ or \,
        i += 2;
        continue;
      }
      const body = macros[m[0]];
      i += m[0].length;
      if (body === undefined) {
        res += m[0];
        continue;
      }
      // Read as many arguments as the body uses: brace groups, or single tokens.
      const nargs = Math.max(0, ...[...body.matchAll(/#(\d)/g)].map((x) => Number(x[1])));
      const args: string[] = [];
      for (let a = 0; a < nargs; a++) {
        while (out[i] === " ") i++;
        if (out[i] === "{") {
          let depth = 0;
          const start = i;
          for (; i < out.length; i++) {
            if (out[i] === "{") depth++;
            else if (out[i] === "}" && --depth === 0) break;
          }
          args.push(out.slice(start + 1, i));
          i++;
        } else {
          const tok = /^\\[A-Za-z]+|^./.exec(out.slice(i));
          args.push(tok ? tok[0] : "");
          i += tok ? tok[0].length : 0;
        }
      }
      res += ` {${body.replace(/#(\d)/g, (_x, d: string) => args[Number(d) - 1] ?? "")}} `;
      changed = true;
    }
    out = res;
    if (!changed || out.length > 20000) break; // done, or a runaway definition
  }
  return out;
}

function prepare(src: string, macros?: Record<string, string>): Node[] {
  const cleaned = src
    .replace(/\\\\/g, " ")
    .replace(/&/g, " ")
    .replace(/\\(?:left|right|big|Big|bigg|Bigg)(?=[^A-Za-z])/g, "")
    .replace(/\\(?:nonumber|notag|label\{[^}]*\})/g, "")
    .replace(/\\begin\{[^}]*\}|\\end\{[^}]*\}/g, " ");
  return parse(tokenize(expandMacros(cleaned, macros)));
}

// --------------------------------------------------------------------------- //
// Shared vocabulary
// --------------------------------------------------------------------------- //

const GREEK: Record<string, [string, string]> = {
  alpha: ["α", "alpha"], beta: ["β", "beta"], gamma: ["γ", "gamma"], delta: ["δ", "delta"],
  epsilon: ["ε", "epsilon"], varepsilon: ["ε", "epsilon"], zeta: ["ζ", "zeta"], eta: ["η", "eta"],
  theta: ["θ", "theta"], vartheta: ["ϑ", "theta"], iota: ["ι", "iota"], kappa: ["κ", "kappa"],
  lambda: ["λ", "lambda"], mu: ["μ", "mu"], nu: ["ν", "nu"], xi: ["ξ", "xi"], pi: ["π", "pi"],
  rho: ["ρ", "rho"], sigma: ["σ", "sigma"], tau: ["τ", "tau"], upsilon: ["υ", "upsilon"],
  phi: ["φ", "phi"], varphi: ["φ", "phi"], chi: ["χ", "chi"], psi: ["ψ", "psi"], omega: ["ω", "omega"],
  Gamma: ["Γ", "capital gamma"], Delta: ["Δ", "capital delta"], Theta: ["Θ", "capital theta"],
  Lambda: ["Λ", "capital lambda"], Xi: ["Ξ", "capital xi"], Pi: ["Π", "capital pi"],
  Sigma: ["Σ", "capital sigma"], Phi: ["Φ", "capital phi"], Psi: ["Ψ", "capital psi"],
  Omega: ["Ω", "capital omega"],
};

/** Symbol commands: [unicode, words]. */
const SYMBOLS: Record<string, [string, string]> = {
  cdot: ["·", "times"], times: ["×", "times"], div: ["÷", "divided by"], pm: ["±", "plus or minus"],
  mp: ["∓", "minus or plus"], leq: ["≤", "is at most"], le: ["≤", "is at most"],
  geq: ["≥", "is at least"], ge: ["≥", "is at least"], neq: ["≠", "is not equal to"],
  ne: ["≠", "is not equal to"], approx: ["≈", "is approximately"], simeq: ["≃", "is approximately"],
  sim: ["∼", "is distributed as"], equiv: ["≡", "is defined as"], propto: ["∝", "is proportional to"],
  ll: ["≪", "is much less than"], gg: ["≫", "is much greater than"], in: ["∈", "in"],
  notin: ["∉", "not in"], subset: ["⊂", "is a subset of"], subseteq: ["⊆", "is a subset of"],
  cup: ["∪", "union"], cap: ["∩", "intersect"], to: ["→", "goes to"], rightarrow: ["→", "goes to"],
  leftarrow: ["←", "comes from"], Rightarrow: ["⇒", "implies"], mapsto: ["↦", "maps to"],
  infty: ["∞", "infinity"], partial: ["∂", "partial"], nabla: ["∇", "the gradient"],
  forall: ["∀", "for all"], exists: ["∃", "there exists"], mid: ["|", "given"],
  parallel: ["‖", "and"], "|": ["‖", "and"], ldots: ["…", "and so on"], cdots: ["⋯", "and so on"],
  dots: ["…", "and so on"], top: ["ᵀ", "transpose"], intercal: ["ᵀ", "transpose"],
  prime: ["′", "prime"], circ: ["∘", "composed with"], odot: ["⊙", "element-wise times"],
  otimes: ["⊗", "tensor"], oplus: ["⊕", "plus"], langle: ["⟨", "the inner product of"],
  rangle: ["⟩", ""], lVert: ["‖", "the norm of"], rVert: ["‖", ""], ell: ["ℓ", "ell"],
  emptyset: ["∅", "the empty set"], neg: ["¬", "not"], land: ["∧", "and"], lor: ["∨", "or"],
};

/** Big operators: [unicode, words]. */
const BIG: Record<string, [string, string]> = {
  sum: ["∑", "the sum"], prod: ["∏", "the product"], int: ["∫", "the integral"],
  oint: ["∮", "the integral"], bigcup: ["⋃", "the union"], bigcap: ["⋂", "the intersection"],
};

/** Named functions read as "<name> of". */
const FUNCS = new Set([
  "log", "ln", "exp", "sin", "cos", "tan", "tanh", "sigmoid", "max", "min", "arg", "argmax",
  "argmin", "sup", "inf", "lim", "det", "tr", "softmax", "relu", "ReLU", "KL", "Var", "Cov",
  "diag", "sign", "sgn", "Pr",
]);

const SPACING = new Set([",", ";", ":", "!", "quad", "qquad", " ", "enspace", "thinspace", "medspace", "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits"]);

const BLACKBOARD: Record<string, string> = { R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ", E: "𝔼", P: "ℙ", I: "𝕀" };
const CALLIGRAPHIC: Record<string, string> = {
  L: "ℒ", N: "𝒩", O: "𝒪", D: "𝒟", X: "𝒳", Y: "𝒴", Z: "𝒵", H: "ℋ", F: "ℱ", E: "ℰ",
  M: "ℳ", B: "ℬ", R: "ℛ", I: "ℐ", A: "𝒜", C: "𝒞", G: "𝒢", P: "𝒫", S: "𝒮", T: "𝒯", U: "𝒰", V: "𝒱", W: "𝒲",
};

const SUB = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ";
const SUB_SRC = "0123456789+-=()aehijklmnoprstuvxβγρφχ";
const SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂᵝᵞᵟᶿᵠᵡ";
const SUP_SRC = "0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVWβγδθφχ";

function scriptChars(text: string, kind: "sub" | "sup"): string | null {
  const [from, to] = kind === "sub" ? [SUB_SRC, [...SUB]] : [SUP_SRC, [...SUP]];
  let out = "";
  for (const ch of text) {
    if (ch === " ") continue;
    const at = [...from].indexOf(ch);
    if (at < 0) return null;
    out += to[at];
  }
  return out;
}

// --------------------------------------------------------------------------- //
// toUnicode
// --------------------------------------------------------------------------- //

/** One token needing no parentheses: letters, digits, scripts, √, accents. */
const SIMPLE = /^[\p{L}\p{N}\p{M}⁰-₟ᵀ-ᵪ′√.]+$/u;

function uni(nodes: Node[]): string {
  return nodes.map(uniNode).join("").replace(/\s+/g, " ").trim();
}

function uniNode(n: Node): string {
  switch (n.k) {
    case "chr":
      return n.v === "~" ? " " : n.v;
    case "grp":
      return uni(n.body);
    case "scr": {
      const base = n.base ? uniNode(n.base).trim() : "";
      let out = base;
      if (n.sub) {
        const s = uni(n.sub);
        out += scriptChars(s, "sub") ?? (s.length === 1 || /^\(.*\)$/.test(s) ? `_${s}` : `_(${s})`);
      }
      if (n.sup) {
        const s = uni(n.sup);
        if (s === "ᵀ" || s === "′") out += s;
        else out += scriptChars(s, "sup") ?? (s.length === 1 ? `^${s}` : `^(${s})`);
      }
      return out;
    }
    case "cmd": {
      const { name, args } = n;
      if (GREEK[name]) return GREEK[name][0];
      if (SYMBOLS[name]) return ` ${SYMBOLS[name][0]} `.replace(/^ (ᵀ|′) $/, "$1");
      if (BIG[name]) return BIG[name][0];
      if (SPACING.has(name)) return " ";
      if (FUNCS.has(name)) return `${name} `;
      const a = args.map(uni);
      switch (name) {
        case "frac": case "dfrac": case "tfrac": case "cfrac": {
          const wrap = (s: string) => (SIMPLE.test(s) ? s : `(${s})`);
          return `${wrap(a[0])}/${wrap(a[1])}`;
        }
        case "sqrt":
          return SIMPLE.test(a[0]) ? `√${a[0]}` : `√(${a[0]})`;
        case "nroot":
          return `${a[0]}√(${a[1]})`;
        case "binom":
          return `C(${a[0]}, ${a[1]})`;
        case "mathbb":
          return [...a[0]].map((c) => BLACKBOARD[c] ?? c).join("");
        case "mathcal": case "mathscr":
          return [...a[0]].map((c) => CALLIGRAPHIC[c] ?? c).join("");
        case "hat": case "widehat":
          return `${a[0]}̂`;
        case "tilde": case "widetilde":
          return `${a[0]}̃`;
        case "bar": case "overline":
          return `${a[0]}̄`;
        case "vec":
          return `${a[0]}⃗`;
        case "dot":
          return `${a[0]}̇`;
        case "textcolor":
          return a[1];
        case "color":
          return "";
        default:
          return a.length ? a.join(" ") : name;
      }
    }
  }
}

export function toUnicode(tex: string, macros?: Record<string, string>): string {
  try {
    return uni(prepare(tex, macros))
      .replace(/([\p{L}\p{N}⁰-₟ᵢ-ᵪ)]) \(/gu, "$1(")
      .replace(/\s+([,.;)\]])/g, "$1")
      .replace(/([([])\s+/g, "$1")
      .replace(/\s{2,}/g, " ")
      .trim();
  } catch {
    return tex.replace(/[\\{}$]/g, "");
  }
}

// --------------------------------------------------------------------------- //
// toWords
// --------------------------------------------------------------------------- //

const CHAR_WORDS: Record<string, string> = {
  "=": "equals", "+": "plus", "-": "minus", "<": "is less than", ">": "is greater than",
  "/": "divided by", "*": "times", "|": "given", ",": ",", "!": "factorial", "'": "prime",
  ":": ":", ";": ";", ".": ".",
};

/** Style wrappers that change how a symbol looks, not what it is. */
const STYLE = new Set(["mathbf", "boldsymbol", "bm", "mathrm", "mathit", "mathsf", "mathtt"]);

function isSimple(nodes: Node[]): boolean {
  if (nodes.length !== 1) return false;
  const n = nodes[0];
  if (n.k === "chr") return true;
  if (n.k === "grp") return isSimple(n.body);
  if (n.k !== "cmd") return false;
  if (!n.args.length) return true;
  return STYLE.has(n.name) && isSimple(n.args[0]);
}

/** `(i)` and `(n)` as superscripts are indices — "the i-th x" — not powers. */
function indexSuperscript(nodes: Node[]): string | null {
  const flat = nodes.length === 1 && nodes[0].k === "grp" ? nodes[0].body : nodes;
  const text = flat.map((n) => (n.k === "chr" ? n.v : "?")).join("");
  const m = /^\(([a-z])\)$/.exec(text);
  return m ? m[1] : null;
}

const chrOf = (n: Node | undefined): string | null => (n && n.k === "chr" ? n.v : null);

/** The next node after `i` that is not a space (spaces mean nothing in math). */
const nextSig = (nodes: Node[], i: number): Node | undefined => {
  let j = i + 1;
  while (j < nodes.length && chrOf(nodes[j]) === " ") j++;
  return nodes[j];
};

function words(nodes: Node[]): string {
  const parts: string[] = [];
  let i = 0;
  while (i < nodes.length) {
    const n = nodes[i];
    const c = chrOf(n);
    // Numbers stay whole: "10000", not "1 0 0 0 0".
    if (c !== null && /[0-9.]/.test(c)) {
      let s = "";
      while (chrOf(nodes[i]) !== null && /[0-9.]/.test(chrOf(nodes[i])!)) s += chrOf(nodes[i++]);
      parts.push(s);
      continue;
    }
    // Letter runs: two letters are a product ("Q K"); three or more are a name
    // ("pos", "softmax") and read as one word.
    if (c !== null && /[A-Za-z]/.test(c)) {
      let s = "";
      while (chrOf(nodes[i]) !== null && /[A-Za-z]/.test(chrOf(nodes[i])!)) s += chrOf(nodes[i++]);
      parts.push(s.length >= 3 ? s : s.split("").join(" "));
      if (chrOf(nextSig(nodes, i - 1)) === "(") parts.push("of"); // f(x) reads "f of x"
      continue;
    }
    const w = wordNode(n);
    if (w) parts.push(w);
    // "log p" reads "log of p", and "f(x)" reads "f of x".
    if (n.k === "cmd" && FUNCS.has(n.name) && n.name !== "exp") parts.push("of");
    else if (chrOf(nextSig(nodes, i)) === "(" && isApplicable(n)) parts.push("of");
    i++;
  }
  return parts
    .join(" ")
    .replace(/\(\s*/g, "")
    .replace(/\s*\)/g, "")
    .replace(/\s+([,.;:])/g, "$1")
    .replace(/\bof of\b/g, "of")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function isApplicable(n: Node): boolean {
  if (n.k === "cmd") return FUNCS.has(n.name) || n.name === "operatorname" || n.name === "mathrm" || (!!GREEK[n.name]) || n.name === "mathcal";
  if (n.k === "chr") return /[A-Za-z]/.test(n.v);
  if (n.k === "scr") return true;
  // An expanded macro arrives as a group: `\pT(x)` is `{p_\theta}(x)`.
  if (n.k === "grp") return n.body.length > 0 && isApplicable(n.body[n.body.length - 1]);
  return false;
}

function wordNode(n: Node): string {
  switch (n.k) {
    case "chr":
      if (/[A-Za-z0-9]/.test(n.v)) return n.v;
      if (n.v === "(" || n.v === ")") return n.v; // stripped later, kept for "of"
      if (n.v === "[" || n.v === "]" || n.v === "{" || n.v === "}" || n.v === " " || n.v === "~") return "";
      return CHAR_WORDS[n.v] ?? n.v;
    case "grp":
      return words(n.body);
    case "scr": {
      const base = n.base ? wordNode(n.base) : "";
      // Big operators with limits: "the sum from i = 1 to n of".
      if (n.base && n.base.k === "cmd" && BIG[n.base.name]) {
        let out = BIG[n.base.name][1];
        if (n.sub) out += ` over ${words(n.sub)}`.replace(/ over (.+?) equals /, " from $1 equals ");
        if (n.sup) out += ` to ${words(n.sup)}`;
        return `${out} of`;
      }
      // Operators whose subscript says what they range over.
      if (n.base && n.base.k === "cmd" && n.sub) {
        if (n.base.name === "nabla") return `the gradient with respect to ${words(n.sub)} of`;
        if (n.base.name === "mathbb" && words(n.base.args[0] ?? []) === "E") {
          return `the expected value under ${words(n.sub)}, of`;
        }
      }
      let out = base;
      if (n.sub) out += isSimple(n.sub) ? ` sub ${words(n.sub)}` : ` sub ${words(n.sub)},`;
      if (n.sup) {
        const s = words(n.sup);
        const index = indexSuperscript(n.sup);
        if (index) out = `the ${index}-th ${out}`;
        else if (s === "2") out += " squared";
        else if (s === "3") out += " cubed";
        else if (s === "transpose" || s === "T") out += " transpose";
        else if (s === "minus 1") out += " inverse";
        else if (s === "prime") out += " prime";
        else out += isSimple(n.sup) ? ` to the ${s}` : ` to the power ${s},`;
      }
      return out;
    }
    case "cmd": {
      const { name, args } = n;
      if (GREEK[name]) return GREEK[name][1];
      if (SYMBOLS[name]) return SYMBOLS[name][1];
      if (BIG[name]) return `${BIG[name][1]} of`;
      if (SPACING.has(name)) return "";
      if (FUNCS.has(name)) return name === "exp" ? "e to the" : name;
      const a = args.map(words);
      switch (name) {
        case "frac": case "dfrac": case "tfrac": case "cfrac":
          return isSimple(args[0]) && isSimple(args[1])
            ? `${a[0]} over ${a[1]}`
            : `the fraction ${a[0]}, over ${a[1]},`;
        case "sqrt":
          return isSimple(args[0]) ? `the square root of ${a[0]}` : `the square root of ${a[0]},`;
        case "nroot":
          return `the ${a[0]}th root of ${a[1]}`;
        case "binom":
          return `${a[0]} choose ${a[1]}`;
        case "mathbb":
          return a[0] === "E" ? "the expected value" : a[0] === "R" ? "the real numbers" : a[0];
        case "mathcal":
          return a[0] === "N" ? "the normal distribution" : a[0] === "L" ? "L" : a[0];
        case "hat": case "widehat":
          return `${a[0]} hat`;
        case "tilde": case "widetilde":
          return `${a[0]} tilde`;
        case "bar": case "overline":
          return `${a[0]} bar`;
        case "vec":
          return `vector ${a[0]}`;
        case "text": case "textrm": case "textit": case "textbf": case "mbox": case "emph":
          return toUnicode(textOf(args[0]));
        case "textcolor":
          return a[1];
        case "color":
          return "";
        case "operatorname": case "mathrm":
          return textOf(args[0]);
        default:
          return a.length ? a.join(" ") : name;
      }
    }
  }
}

function textOf(nodes: Node[]): string {
  return nodes.map((n) => (n.k === "chr" ? n.v : n.k === "grp" ? textOf(n.body) : uniNode(n))).join("");
}

export function toWords(tex: string, macros?: Record<string, string>): string {
  try {
    const out = words(prepare(tex, macros))
      .replace(/,\s*(equals|plus|minus|times|over|given)/g, " $1")
      .replace(/,+/g, ",")
      .replace(/^,|,$/g, "")
      .trim();
    return out ? out[0].toUpperCase() + out.slice(1) : "";
  } catch {
    return "";
  }
}

// --------------------------------------------------------------------------- //
// Text with math in it
// --------------------------------------------------------------------------- //

export type Piece = { math: false; text: string } | { math: true; tex: string };

/** Split model-written text into words and `$…$` / `\(…\)` math. */
export function splitMath(text: string): Piece[] {
  const out: Piece[] = [];
  const re = /\$\$([^$]+)\$\$|\$([^$\n]+)\$|\\\((.+?)\\\)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ math: false, text: text.slice(last, at) });
    out.push({ math: true, tex: m[1] ?? m[2] ?? m[3] ?? "" });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ math: false, text: text.slice(last) });
  return out;
}

/**
 * Tidy pseudo-math that models type into plain sentences without `$`:
 * `sqrt(d_k)` → `√(dₖ)`, `W_1` → `W₁`, `x^2` → `x²`, `∇_φ L` → `∇ᵩ L`.
 * Only short tokens are touched, so `snake_case` words survive.
 */
export function prettifyPlainMath(text: string): string {
  return text
    .replace(/\bsqrt\(/g, "√(")
    .replace(/(^|[^\w\\])([A-Za-zͰ-Ͽ∇]{1,3})_\{?([A-Za-z0-9Ͱ-Ͽ+-]{1,4})\}?(?![A-Za-z0-9_])/gu, (m, pre, base, sub) => {
      const s = scriptChars(sub, "sub");
      return s ? `${pre}${base}${s}` : m;
    })
    .replace(/(^|[^\w\\])([A-Za-z0-9Ͱ-Ͽ)]{1,3})\^\{?([A-Za-z0-9+-]{1,3})\}?(?![A-Za-z0-9^])/gu, (m, pre, base, sup) => {
      const s = scriptChars(sup, "sup");
      return s ? `${pre}${base}${s}` : m;
    });
}

/** Plain readable text, for places that cannot hold KaTeX (titles, SVG, the outline). */
export function plainText(text: string, macros?: Record<string, string>): string {
  return splitMath(text)
    .map((p) => (p.math ? toUnicode(p.tex, macros) : prettifyPlainMath(p.text)))
    .join("")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** Whether a string carries TeX that should not be shown as-is. */
export function hasTex(text: string): boolean {
  return /\$|\\[A-Za-z]|[_^]\{/.test(text);
}
