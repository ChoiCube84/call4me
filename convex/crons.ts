import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";
const crons = cronJobs();
crons.interval("expire live replay", { minutes: 5 }, internal.live.expire);
export default crons;
