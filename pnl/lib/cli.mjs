import { realpath } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

/** Match Node's resolved module URL without running a library import as a CLI. */
export async function isCliEntry(moduleUrl, entry = process.argv[1]) {
  if (!entry) return false;
  try { return moduleUrl === pathToFileURL(await realpath(resolve(entry))).href; }
  catch (cause) {
    // eval/import callers may have no real entry file. Unexpected filesystem
    // failures must reach the caller's structured CLI error handler instead.
    if (cause.code === 'ENOENT' || cause.code === 'ENOTDIR') return false;
    throw cause;
  }
}
