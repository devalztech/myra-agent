import { useEffect, useState } from "react";

/**
 * True once React has hydrated on the client.
 *
 * Auth forms are server-rendered, so a very fast user (or an automated test)
 * can press Submit before React attaches its handlers. Without this gate the
 * browser performs a native GET submit and the credentials are dropped.
 */
export function useHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  return hydrated;
}
