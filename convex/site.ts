import { action, internalMutation, internalQuery } from "./_generated/server";
import { internal } from "./_generated/api";
import { v } from "convex/values";
import { requireWorker } from "./lib/auth";

export const asset = internalQuery({
  args: { path: v.string() },
  handler: (ctx, { path }) =>
    ctx.db.query("site_asset").withIndex("by_path", q => q.eq("path", path)).unique(),
});

export const replaceAsset = internalMutation({
  args: { path: v.string(), storage_id: v.id("_storage"), content_type: v.string() },
  handler: async (ctx, args) => {
    const prior = await ctx.db.query("site_asset")
      .withIndex("by_path", q => q.eq("path", args.path)).unique();
    if (prior) {
      await ctx.db.patch(prior._id, {
        storage_id: args.storage_id, content_type: args.content_type,
      });
      await ctx.storage.delete(prior.storage_id);
    } else {
      await ctx.db.insert("site_asset", args);
    }
  },
});

// The build uploader runs once from the developer's machine. Runtime requests are
// served from Convex storage, so the machine can be switched off afterward.
export const upload = action({
  args: {
    token: v.string(), path: v.string(), content_type: v.string(),
    bytes_base64: v.string(),
  },
  handler: async (ctx, args) => {
    requireWorker(args.token);
    if (!args.path.startsWith("/") || args.path.includes("..")) throw Error("Invalid path");
    const binary = atob(args.bytes_base64);
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
    const storage_id = await ctx.storage.store(new Blob([bytes], { type: args.content_type }));
    await ctx.runMutation(internal.site.replaceAsset, {
      path: args.path, storage_id, content_type: args.content_type,
    });
    return null;
  },
});
