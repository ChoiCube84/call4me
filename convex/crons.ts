import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";
const crons = cronJobs();
crons.interval("expire live replay", { minutes: 5 }, internal.live.expire);
crons.interval("expire encrypted answers", { seconds: 30 }, internal.managed.expireAnswers);
crons.interval("delete finished call data", { minutes: 5 }, internal.maintenance.deleteFinishedCallData);
crons.interval("poll active calls", { minutes: 1 }, internal.maintenance.pollActiveCalls);
export default crons;
