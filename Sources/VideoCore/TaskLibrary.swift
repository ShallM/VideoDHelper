import Foundation

public struct TaskProject: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public var name: String
    public let createdAt: Double
}

public enum TaskCollection: Hashable, Sendable {
    case recent
    case unassigned
    case project(String)
}

/// Organization is separate from worker-owned job records, so progress writes
/// cannot undo a move. Removing a project never touches task folders or results.
public struct TaskLibrary: Codable, Sendable {
    public private(set) var projects: [TaskProject] = []
    public private(set) var assignments: [String: String] = [:]
    public init() {}

    public static var fileURL: URL { AppStorage.root.appendingPathComponent("library.json") }

    public static func load(from url: URL = fileURL) throws -> TaskLibrary {
        guard FileManager.default.fileExists(atPath: url.path) else { return TaskLibrary() }
        let library = try AppStorage.read(TaskLibrary.self, from: url)
        guard Set(library.projects.map(\.id)).count == library.projects.count,
              library.projects.allSatisfy({ UUID(uuidString: $0.id) != nil && !$0.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            throw VideoError.toolFailed("项目目录数据无效；原始任务与结果仍保存在本机。")
        }
        return library
    }

    public func save(to url: URL = fileURL) throws { try AppStorage.write(self, to: url) }

    public func project(for taskID: String) -> TaskProject? {
        guard let id = assignments[taskID] else { return nil }
        return projects.first { $0.id == id }
    }

    @discardableResult
    public mutating func createProject(name: String) throws -> TaskProject {
        let project = TaskProject(id: UUID().uuidString, name: try checkedName(name), createdAt: Date().timeIntervalSince1970)
        projects.append(project)
        return project
    }

    public mutating func renameProject(_ id: String, name: String) throws {
        guard let index = projects.firstIndex(where: { $0.id == id }) else { throw VideoError.toolFailed("找不到这个项目。") }
        projects[index].name = try checkedName(name, excluding: id)
    }

    public mutating func removeProject(_ id: String) {
        projects.removeAll { $0.id == id }
        assignments = assignments.filter { $0.value != id }
    }

    public mutating func moveTask(_ taskID: String, to projectID: String?) throws {
        guard UUID(uuidString: taskID) != nil else { throw VideoError.toolFailed("无效任务编号。") }
        if let projectID, !projects.contains(where: { $0.id == projectID }) { throw VideoError.toolFailed("找不到目标项目。") }
        assignments[taskID] = projectID
    }

    public func tasks(_ jobs: [ProcessingJob], in collection: TaskCollection, matching query: String = "") -> [ProcessingJob] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return jobs.filter { job in
            let included: Bool
            switch collection {
            case .recent: included = true
            case .unassigned: included = project(for: job.id) == nil
            case .project(let id): included = project(for: job.id)?.id == id
            }
            return included && (query.isEmpty || job.title.localizedCaseInsensitiveContains(query) || job.source.localizedCaseInsensitiveContains(query))
        }.sorted { $0.updatedAt == $1.updatedAt ? $0.id < $1.id : $0.updatedAt > $1.updatedAt }
    }

    private func checkedName(_ value: String, excluding id: String? = nil) throws -> String {
        let name = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 40, name.rangeOfCharacter(from: .controlCharacters) == nil else {
            throw VideoError.toolFailed("项目名称需为 1～40 个字符，不能包含换行。")
        }
        guard !projects.contains(where: { $0.id != id && $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame }) else {
            throw VideoError.toolFailed("已有同名项目，请换一个名称。")
        }
        return name
    }
}
