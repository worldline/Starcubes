import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

/**
 * Runs one of the app's classic scripts in the jsdom global scope.
 *
 * The files are plain `<script>` sources, not ES modules: they declare globals
 * (`class DateTimePicker`, `var ROW_SPECS`) and assign to `window`. Loading
 * them through indirect eval reproduces that, and `expose` republishes the
 * lexical declarations the suite needs afterwards.
 */
export function loadScript(relativePath, expose = []) {
  let code = fs.readFileSync(path.join(ROOT, relativePath), 'utf8');
  if (expose.length) {
    code += '\n;(() => {\n'
      + expose.map((name) => `globalThis.${name} = ${name};`).join('\n')
      + '\n})();';
  }
  (0, eval)(code);
}

export function resetDom() {
  document.body.innerHTML = '';
  document.head.innerHTML = '';
}
