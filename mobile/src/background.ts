import * as BackgroundTask from "expo-background-task";
import * as TaskManager from "expo-task-manager";
import { runBackup } from "./backup";
import { loadBackupPolicy } from "./policies";

export const BACKUP_TASK = "drivebound-background-backup";
TaskManager.defineTask(BACKUP_TASK, async () => {
  try {
    const policy = await loadBackupPolicy();
    if (!policy.automatic) return BackgroundTask.BackgroundTaskResult.Success;
    await runBackup();
    return BackgroundTask.BackgroundTaskResult.Success;
  }
  catch { return BackgroundTask.BackgroundTaskResult.Failed; }
});

export async function setAutomaticBackup(enabled: boolean) {
  const registered = await TaskManager.isTaskRegisteredAsync(BACKUP_TASK);
  if (enabled && !registered) await BackgroundTask.registerTaskAsync(BACKUP_TASK, { minimumInterval: 15 });
  else if (!enabled && registered) await BackgroundTask.unregisterTaskAsync(BACKUP_TASK);
}
