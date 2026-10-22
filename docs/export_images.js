// Writes docs/img/*.svg (step icons + step pictures) from the drawing code inside
// migration_logic.html, so the .md file shows exactly the same pictures as the page.
//   node docs/export_images.js
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "migration_logic.html"), "utf8");
const m = html.match(/\/\*ILL-START\*\/([\s\S]*?)\/\*ILL-END\*\//);
if (!m) throw new Error("ILL-START / ILL-END block not found in migration_logic.html");
const { ICONS, ILL } = new Function(m[1] + "\nreturn { ICONS: ICONS, ILL: ILL };")();

// Light palette (same values as :root in the page); --line differs per line.
const BASE = {
  bg: "#f2f4f5", surface: "#ffffff", fg: "#142029", muted: "#5b6a75", rule: "#d5dce0",
  lineA: "#0b7f86", lineASoft: "#d9eff0", lineB: "#b8650a", lineBSoft: "#f6e6d2", ok: "#2f7d3b", warn: "#a23a2a",
};
const css = (line, soft) => `
  .ill{background:${BASE.bg}}
  text{font-family:Consolas,"IBM Plex Mono","Courier New",monospace;font-size:9.5px;fill:${BASE.fg}}
  text.lbl{font-family:"Barlow Condensed","Arial Narrow",Arial,sans-serif;font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}
  text.mut{fill:${BASE.muted}} text.acc{fill:${line}} text.ok{fill:${BASE.ok}} text.bad{fill:${BASE.warn}}
  .s{fill:${BASE.surface};stroke:${line};stroke-width:1.6} .s2{fill:${soft};stroke:${line};stroke-width:1.6}
  .sf{fill:${line};stroke:${line};stroke-width:1.6} .n{fill:${BASE.surface};stroke:${BASE.muted};stroke-width:1.2}
  .x{fill:${BASE.bg};stroke:${BASE.muted};stroke-width:1;stroke-dasharray:3 2}
  .ln{fill:none;stroke:${BASE.muted};stroke-width:1.6} .lna{fill:none;stroke:${line};stroke-width:2}
  .dash{fill:none;stroke:${line};stroke-width:1.6;stroke-dasharray:4 3} .hd{fill:${BASE.muted}} .hda{fill:${line}}
  .okl{fill:none;stroke:${BASE.ok};stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
  .badl{fill:none;stroke:${BASE.warn};stroke-width:2.4;stroke-linecap:round}
  .cA{fill:${BASE.lineASoft};stroke:${BASE.lineA};stroke-width:1.2} .cB{fill:${BASE.lineBSoft};stroke:${BASE.lineB};stroke-width:1.2}`;

const NAMES = {
  a: ["generate", "create", "schema", "attach", "preflight", "merge", "finalize", "verify", "detach"],
  b: ["read", "sort-headers", "filter", "stage", "match", "horizontal", "vertical", "commit"],
};

const out = path.join(__dirname, "img");
fs.mkdirSync(out, { recursive: true });
let n = 0;
for (const line of ["a", "b"]) {
  const color = line === "a" ? BASE.lineA : BASE.lineB;
  const soft = line === "a" ? BASE.lineASoft : BASE.lineBSoft;
  NAMES[line].forEach((name, i) => {
    const id = `${line}${i + 1}-${name}`;
    // picture: add a background rect + embedded style, fixed pixel size for Markdown viewers
    const pic = ILL[line][i]()
      .replace('<svg class="ill" viewBox="0 0 360 130"',
               `<svg class="ill" viewBox="0 0 360 130" width="720" height="260"`)
      .replace(/(<svg[^>]*>)/, `$1<style>${css(color, soft)}</style><rect width="360" height="130" fill="${BASE.bg}"/>`);
    fs.writeFileSync(path.join(out, `${id}.svg`), pic + "\n");
    const ico = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" ` +
                `stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[line][i]}</svg>\n`;
    fs.writeFileSync(path.join(out, `icon-${id}.svg`), ico);
    n += 2;
  });
}
console.log(`wrote ${n} files to ${out}`);
