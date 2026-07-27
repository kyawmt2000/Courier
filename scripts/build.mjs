import { cp, mkdir, readFile, rm, writeFile, copyFile } from "node:fs/promises";

const html = await readFile("index.html", "utf8");

await rm("dist", { recursive: true, force: true });
await mkdir("dist/server", { recursive: true });
await mkdir("dist/privacy", { recursive: true });
await mkdir("dist/.openai", { recursive: true });
await copyFile("index.html", "dist/index.html");
await copyFile("privacy/index.html", "dist/privacy/index.html");
await copyFile(".openai/hosting.json", "dist/.openai/hosting.json");
await cp("assets", "dist/assets", { recursive: true });
await cp("downloads", "dist/downloads", { recursive: true });

const server = `const html = ${JSON.stringify(html)};

export default {
  async fetch() {
    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300"
      }
    });
  }
};
`;

await writeFile("dist/server/index.js", server);
