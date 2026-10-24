import { ConvexError } from "convex/values";

function requireSecret(given: string, name: string) {
  const expected = process.env[name];
  // Missing configuration always fails closed, including development.
  if (!expected || !given || given !== expected) throw new ConvexError("UNAUTHORIZED");
}
export function requireWorker(token: string) { requireSecret(token, "CALL4ME_BACKEND_TOKEN"); }
export function requireViewer(accessCode: string) { requireSecret(accessCode, "CALL4ME_VIEWER_TOKEN"); }
