/**
 * The object whose `setItem` a test should spy on to make `localStorage` writes fail.
 *
 * Under real jsdom the Storage methods live on `Storage.prototype` and own-property spies are
 * ignored. On Node 26 `vitest.setup.ts` replaces an unusable global with a plain-object shim whose
 * methods are OWN properties, and `Storage.prototype` is never consulted. Pick whichever the live
 * `localStorage` actually dispatches through.
 */
export function storageWriteTarget(): Storage {
  return Object.prototype.hasOwnProperty.call(localStorage, 'setItem') ? localStorage : Storage.prototype
}
