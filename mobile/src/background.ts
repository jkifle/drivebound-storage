import * as BackgroundTask from "expo-background-task";
import * as TaskManager from "expo-task-manager";
import { runBackup } from "./backup";

export const BACKUP_TASK = "drivebound-background-backup";
TaskManager.defineTask(BACKUP_TASK, async () => {
  try { await runBackup(); return BackgroundTask.BackgroundTaskResult.Success; }
  catch { return BackgroundTask.BackgroundTaskResult.Failed; }
});

export async function setAutomaticBackup(enabled: boolean) {
  if (enabled) await BackgroundTask.registerTaskAsync(BACKUP_TASK, { minimumInterval: 15 });
  else await BackgroundTask.unregisterTaskAsync(BACKUP_TASK);
}
