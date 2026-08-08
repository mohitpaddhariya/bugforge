"use client";

/**
 * Boots the browser telemetry tracker exactly once per page load.
 *
 * Mount it high in the tree (root layout) so the document-level capture-phase
 * click listener is installed before anything interactive renders. It renders
 * its children untouched and adds no DOM of its own.
 *
 *   <TelemetryProvider>{children}</TelemetryProvider>
 *   <TelemetryProvider />                       // also fine
 *   <TelemetryProvider userId={user.id} />      // bind a known user
 */

import { useEffect, useRef, type ReactNode } from "react";
import { initTelemetry, setTelemetryUser, track } from "../lib/telemetry";
import { me } from "../lib/api";

/**
 * Install during the first client render rather than in an effect, so a click
 * that happens before hydration settles is still captured.
 */
if (typeof window !== "undefined") {
  initTelemetry();
}

export interface TelemetryProviderProps {
  children?: ReactNode;
  /** Known logged-in user id. Skips the identify request when provided. */
  userId?: number | null;
  /** Set false to never call /api/me for identification. */
  identify?: boolean;
}

export function TelemetryProvider({
  children,
  userId,
  identify = true,
}: TelemetryProviderProps) {
  const identified = useRef(false);

  useEffect(() => {
    initTelemetry();
  }, []);

  useEffect(() => {
    if (typeof userId === "number") {
      setTelemetryUser(userId);
      identified.current = true;
      return;
    }
    if (userId === null) {
      setTelemetryUser(null);
      return;
    }
    if (!identify || identified.current) return;
    identified.current = true;

    let cancelled = false;
    me()
      .then((res) => {
        if (cancelled) return;
        const id = res && res.user ? res.user.id : null;
        setTelemetryUser(typeof id === "number" ? id : null);
        if (typeof id === "number") {
          track("business", "identify", {
            user_id: id,
            email: res.user.email,
            locale: res.user.locale,
          });
        }
      })
      .catch(() => {
        // Anonymous visitor, or the API is down. Either way telemetry keeps
        // flowing with user_id = null; traces still join on session_id.
        if (!cancelled) setTelemetryUser(null);
      });

    return () => {
      cancelled = true;
    };
  }, [userId, identify]);

  return <>{children}</>;
}

export default TelemetryProvider;
