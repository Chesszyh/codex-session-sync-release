import { build } from "esbuild";
import { mkdir, copyFile, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
await mkdir("dist", { recursive: true });
const license = await readFile("src/vendor/codex-trace/LICENSE", "utf8");
await build({ banner: { js: `/*! codex-trace\n${license}*/` }, entryPoints: ["src/app.tsx"], bundle: true, format: "esm", outfile: "dist/app.js", minify: true, jsx: "automatic", define: { "process.env.NODE_ENV": '"production"' } });
await copyFile("src/index.html", "dist/index.html");
await writeFile("dist/style.css", (await readFile("src/vendor/codex-trace/src/styles/global.css", "utf8")) + "\n" + (await readFile("src/style.css", "utf8")));
// Asset identity changes the offline cache only when its content changes.
const assets = await Promise.all(["app.js", "style.css", "index.html"].map(n => readFile(`dist/${n}`)));
const version = createHash("sha256").update(Buffer.concat(assets)).digest("hex").slice(0, 16);
await writeFile("dist/sw.js", (await readFile("src/sw.js", "utf8")).replace("__ASSET_VERSION__", version));
