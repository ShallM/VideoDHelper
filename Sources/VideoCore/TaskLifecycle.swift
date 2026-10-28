import Foundation
import Darwin

/// Uses the worker's advisory lock, not its last persisted state: a task marked
/// "running" may simply have been interrupted before its final progress write.
public enum TaskLifecycle {
    public static func setArchived(_ archived: Bool, taskID: String, root: URL = AppStorage.root) throws -> TaskLibrary {
        try withIdleTask(taskID, root: root) { _ in
            var library = try TaskLibrary.load(from: root.appendingPathComponent("library.json"))
            try library.setArchived(archived, taskID: taskID)
            try library.save(to: root.appendingPathComponent("library.json"))
            return library
        }
    }

    public static func trash(taskID: String, root: URL = AppStorage.root) throws -> TaskLibrary {
        try trash(taskID: taskID, root: root, moveToTrash: { folder in
            var destination: NSURL?
            try FileManager.default.trashItem(at: folder, resultingItemURL: &destination)
            guard let destination else { throw VideoError.toolFailed("任务已移到废纸篓，但无法取得恢复位置。请刷新任务列表。") }
            return destination as URL
        })
    }

    // The injected move lets tests verify failures and recovery without touching
    // the user's real Trash. Production always uses FileManager.trashItem.
    static func trash(taskID: String, root: URL, moveToTrash: (URL) throws -> URL) throws -> TaskLibrary {
        try withIdleTask(taskID, root: root) { folder in
            let libraryURL = root.appendingPathComponent("library.json")
            var library = try TaskLibrary.load(from: libraryURL)
            // Fail before moving any files if the organization cannot be saved.
            try library.save(to: libraryURL)
            try UsageRepository.preserveTaskHistory(taskID: taskID, root: root)
            let destination = try moveToTrash(folder)
            library.forgetTask(taskID)
            do { try library.save(to: libraryURL) }
            catch {
                do { try FileManager.default.moveItem(at: destination, to: folder) }
                catch { throw VideoError.toolFailed("任务已在废纸篓中，但目录更新失败。请刷新列表；可在 Finder 废纸篓中恢复任务。") }
                throw VideoError.toolFailed("任务目录保存失败，已将任务放回原处，请稍后重试。")
            }
            return library
        }
    }

    private static func withIdleTask<T>(_ taskID: String, root: URL, operation: (URL) throws -> T) throws -> T {
        guard UUID(uuidString: taskID) != nil else { throw VideoError.toolFailed("无效任务编号。") }
        let folder = root.appendingPathComponent("tasks", isDirectory: true).appendingPathComponent(taskID, isDirectory: true)
        let values = try folder.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw VideoError.toolFailed("任务目录无效，未修改任何任务文件。")
        }
        let lockURL = folder.appendingPathComponent("run.lock")
        let descriptor = open(lockURL.path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw VideoError.toolFailed("无法检查任务是否正在处理，请刷新后重试。") }
        defer { close(descriptor) }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            throw VideoError.toolFailed("这个任务仍在后台处理。请先取消并等待保存完成，再归档或删除。")
        }
        defer { flock(descriptor, LOCK_UN) }
        // A competing deletion can move the directory between open and flock.
        var held = stat(), current = stat()
        guard fstat(descriptor, &held) == 0, lstat(lockURL.path, &current) == 0,
              held.st_dev == current.st_dev, held.st_ino == current.st_ino else {
            throw VideoError.toolFailed("任务位置已变化，请刷新任务列表。")
        }
        let job = try AppStorage.read(ProcessingJob.self, from: folder.appendingPathComponent("job.json"))
        guard job.id == taskID else { throw VideoError.toolFailed("任务目录与记录不匹配。") }
        return try operation(folder)
    }
}
