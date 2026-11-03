import SwiftUI
import VideoCore

struct ProcessingStatusView: View {
    let activity: ProcessingActivity
    let recording: Bool
    let cancel: () -> Void
    @State private var showsSteps = false
    private var cancelling: Bool { activity.outcome == .cancelling }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text(cancelling ? "正在取消并保存进度" : ProcessingActivity.title(for: activity.current.stage, recording: recording))
                    .font(.system(size: 14, weight: .semibold))
                Spacer(minLength: 8)
                Button(cancelling ? "正在取消…" : "取消处理", action: cancel)
                    .disabled(cancelling).controlSize(.small)
            }
            Text(cancelling ? "正在结束当前处理。已完成的阶段会保留，请等待收尾完成。" : activity.current.message)
                .font(.system(size: 13)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            if !cancelling, let progress = activity.progress {
                HStack(spacing: 10) {
                    ProgressView(value: progress).tint(.teal)
                    Text("当前阶段 \(Int(progress * 100))%")
                        .font(.system(size: 12)).monospacedDigit().foregroundStyle(.secondary)
                }.accessibilityElement(children: .combine)
            }
            HStack(alignment: .top) {
                ActivityClock(activity: activity)
                Spacer(minLength: 8)
                Button { showsSteps.toggle() } label: {
                    Label(showsSteps ? "收起阶段记录" : "阶段记录", systemImage: showsSteps ? "chevron.up" : "chevron.down")
                }.buttonStyle(.borderless).font(.system(size: 12))
            }
            if showsSteps {
                ScrollView {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(activity.steps) { step in
                            HStack(alignment: .top, spacing: 10) {
                                Text(ProcessingActivity.duration(step.startedAt.timeIntervalSince(activity.startedAt)))
                                    .monospacedDigit().foregroundStyle(.secondary).frame(width: 58, alignment: .leading)
                                Text(ProcessingActivity.title(for: step.stage, recording: recording))
                                if step.id == activity.current.id { Text("当前").foregroundStyle(.teal) }
                            }.font(.system(size: 12)).help(step.message)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 100)
            }
        }.padding(14)
            .background(Color.teal.opacity(0.055), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.teal.opacity(0.16)))
            .padding(.horizontal, 20).padding(.vertical, 12)
    }
}

/// Only these timer labels redraw every second, not the transcript or task list.
private struct ActivityClock: View {
    let activity: ProcessingActivity
    var body: some View {
        TimelineView(.periodic(from: activity.startedAt, by: 1)) { context in
            VStack(alignment: .leading, spacing: 4) {
                Text("已用 \(ProcessingActivity.duration(activity.elapsed(at: context.date))) · 本阶段 \(ProcessingActivity.duration(context.date.timeIntervalSince(activity.current.startedAt)))")
                    .monospacedDigit()
                if activity.outcome == .running && activity.progress == nil {
                    Text("本阶段未提供百分比，耗时不代表剩余时间。")
                }
            }.font(.system(size: 12)).foregroundStyle(.secondary)
        }
    }
}
