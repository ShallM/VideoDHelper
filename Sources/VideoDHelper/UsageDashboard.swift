import SwiftUI
import Charts
import AppKit
import UniformTypeIdentifiers
import VideoCore

private func tokenText(_ value: Int?) -> String { value.map { $0.formatted() } ?? "—" }
private func rateText(_ value: Double?) -> String { value.map { String(format: "%.1f%%", $0 * 100) } ?? "—" }
private func moneyText(_ value: Double, currency: String) -> String {
    let symbol = ["CNY": "¥", "USD": "$", "EUR": "€", "HKD": "HK$", "JPY": "JP¥"][currency] ?? currency
    return symbol + String(format: value == 0 || value >= 0.01 ? "%.4f" : "%.6f", value)
}
private func costText(_ totals: UsageTotals) -> String {
    totals.costs.keys.sorted().map { moneyText(totals.costs[$0]!, currency: $0) }.joined(separator: " · ")
}

private struct DailyTokens: Identifiable {
    let day: Date
    let kind: String
    let tokens: Int
    var id: String { "\(day.timeIntervalSince1970)-\(kind)" }
}
private struct UsageGroup: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let totals: UsageTotals
}

struct UsageDashboard: View {
    @ObservedObject var editor: PreferencesEditor
    let preferences: AppPreferences
    @State private var period = "30"
    @State private var providerID = "all"
    @State private var sourceFilter: UsageSourceFilter = .all
    @State private var search = ""
    @State private var grouping = "tasks"
    @State private var currentPrices = false
    @State private var rowLimit = 100
    @State private var detail: UsageEvent?
    @State private var showsPricing = false

    private var since: Date? {
        guard let days = Int(period) else { return nil }
        return Calendar.current.date(byAdding: .day, value: -(days - 1), to: Calendar.current.startOfDay(for: Date()))
    }
    private var events: [UsageEvent] {
        editor.snapshot.events.filter {
            sourceFilter.includes($0) && (since == nil || $0.timestamp >= since!.timeIntervalSince1970) &&
            (providerID == "all" || ($0.providerID ?? "legacy") == providerID) &&
            (search.isEmpty || "\($0.displayTask) \($0.displayModel) \($0.displayProvider) \($0.modelID ?? "")".localizedCaseInsensitiveContains(search))
        }
    }
    private var totals: UsageTotals { UsageTotals(events: events, preferences: preferences, currentPrices: currentPrices) }
    private var providerChoices: [(String, String)] {
        var values: [String: String] = [:]
        for event in editor.snapshot.events { values[event.providerID ?? "legacy"] = event.displayProvider }
        return values.sorted { $0.value < $1.value }.map { ($0.key, $0.value) }
    }
    private var daily: [DailyTokens] {
        let calendar = Calendar.current, today = Calendar.current.startOfDay(for: Date())
        let lower = calendar.date(byAdding: .day, value: -89, to: today)!
        let first = max(lower, since ?? calendar.date(byAdding: .day, value: -6, to: min(today, Date(timeIntervalSince1970: events.last?.timestamp ?? Date().timeIntervalSince1970)))!)
        var values: [Date: (Int, Int)] = [:]
        for event in events where !event.usage.invalid {
            let date = calendar.startOfDay(for: Date(timeIntervalSince1970: event.timestamp))
            let old = values[date] ?? (0, 0)
            values[date] = (old.0 + (event.usage.inputTokens ?? 0), old.1 + (event.usage.outputTokens ?? 0))
        }
        var result: [DailyTokens] = [], date = first
        while date <= today {
            result.append(DailyTokens(day: date, kind: "输入", tokens: values[date]?.0 ?? 0))
            result.append(DailyTokens(day: date, kind: "输出", tokens: values[date]?.1 ?? 0))
            date = calendar.date(byAdding: .day, value: 1, to: date)!
        }
        return result
    }
    private var groups: [UsageGroup] {
        let grouped = Dictionary(grouping: events) { event in
            grouping == "models" ? "\(event.providerID ?? "legacy")/\(event.modelID ?? "unknown")" : (event.taskID ?? "unassociated/\(event.purpose)")
        }
        return grouped.map { key, rows in
            UsageGroup(id: key, title: grouping == "models" ? rows[0].displayModel : rows[0].displayTask,
                       subtitle: grouping == "models" ? rows[0].displayProvider : "\(rows[0].sourceLabel) · \(rows.count) 次请求",
                       totals: UsageTotals(events: rows, preferences: preferences, currentPrices: currentPrices))
        }.sorted { ($0.totals.input + $0.totals.output) > ($1.totals.input + $1.totals.output) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                filters
                metrics
                cacheSummary
                if editor.snapshot.unreadableRecords > 0 {
                    Label("\(editor.snapshot.unreadableRecords) 条记录无法读取，暂未计入统计；刷新可重新检查。", systemImage: "exclamationmark.circle").font(.caption).foregroundStyle(.orange)
                }
                if events.contains(where: { $0.providerID == nil }) {
                    Text("旧版日志中的响应已纳入 Token 统计。由于当时未保存服务商、模型和单价，历史费用保持未知。").font(.caption).foregroundStyle(.secondary)
                }
                if events.isEmpty {
                    ContentUnavailableView(search.isEmpty && providerID == "all" && sourceFilter == .all ? "这个时间段还没有请求" : "没有匹配的记录", systemImage: "chart.bar", description: Text("生成视频笔记、整理录音或测试模型后，用量会出现在这里。"))
                        .frame(maxWidth: .infinity).frame(height: 270)
                } else {
                    trend
                    breakdown
                }
                Text("费用为本机价格表的估算，不是服务商账单。输入包含缓存 Token，输出包含推理 Token；本地检查点复用不会产生新请求。中断、网络失败或缺少用量的请求不会被算作免费。").font(.caption).foregroundStyle(.secondary).lineSpacing(4)
            }.padding(.horizontal, 26).padding(.bottom, 24)
        }
        .onAppear { editor.refreshUsage() }
        .onChange(of: sourceFilter) { _, _ in rowLimit = 100 }
        .onReceive(Timer.publish(every: 10, on: .main, in: .common).autoconnect()) { _ in editor.refreshUsage() }
        .sheet(item: $detail) { event in UsageDetailSheet(event: event, preferences: preferences, currentPrices: currentPrices) }
        .sheet(isPresented: $showsPricing) {
            ModelPricingSheet(preferences: preferences) { changes in
                try editor.savePrices(changes)
                currentPrices = true
            }
        }
    }

    private var filters: some View {
        VStack(spacing: 12) {
            HStack(spacing: 12) {
                Text("用量来源").font(.caption).foregroundStyle(.secondary)
                Picker("用量来源", selection: $sourceFilter) {
                    ForEach(UsageSourceFilter.allCases) { source in Text(source.label).tag(source) }
                }.labelsHidden().pickerStyle(.segmented).frame(width: 380)
                Spacer()
            }
            HStack(spacing: 12) {
                Picker("时间范围", selection: $period) { Text("今天").tag("1"); Text("近 7 天").tag("7"); Text("近 30 天").tag("30"); Text("全部").tag("all") }.labelsHidden().frame(width: 130)
                Picker("服务商筛选", selection: $providerID) {
                    Text("全部服务商").tag("all")
                    ForEach(providerChoices, id: \.0) { item in Text(item.1).tag(item.0) }
                }.labelsHidden().frame(width: 180)
                Spacer()
                Button { showsPricing = true } label: { Label("编辑模型价格", systemImage: "tag") }.disabled(editor.isConnecting)
                if editor.isLoadingUsage { ProgressView().controlSize(.small) }
                Button(action: editor.refreshUsage) { Image(systemName: "arrow.clockwise") }.accessibilityLabel("刷新用量")
                Button(action: export) { Label("导出 CSV", systemImage: "square.and.arrow.up") }.disabled(events.isEmpty)
            }
            HStack(spacing: 16) {
                HStack { Image(systemName: "magnifyingglass").foregroundStyle(.secondary); TextField("搜索任务、模型或服务商", text: $search).textFieldStyle(.plain) }
                    .padding(9).background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                Picker("计价方式", selection: $currentPrices) { Text("调用时价格").tag(false); Text("当前价格预估").tag(true) }.labelsHidden().frame(width: 170)
            }
        }
    }
    private var metrics: some View {
        let total = totals
        return HStack(spacing: 12) {
            metric("模型请求", value: total.calls.formatted(), detail: "\(total.unknownUsageCalls) 次用量未知", icon: "arrow.up.arrow.down", color: .teal)
            metric("输入 Tokens", value: total.reportedInputCalls == 0 ? "—" : total.input.formatted(), detail: "\(total.reportedInputCalls) 次返回输入用量", icon: "arrow.up.right", color: .teal)
            metric("输出 Tokens", value: total.reportedOutputCalls == 0 ? "—" : total.output.formatted(), detail: total.reportedReasoningCalls > 0 ? "其中推理 \(total.reasoning.formatted())" : "推理用量未返回", icon: "arrow.down.left", color: .indigo)
            metric("已定价费用估算", value: total.costs.isEmpty ? "待定价" : total.costs.count == 1 ? costText(total) : "\(total.costs.count) 种币种",
                   detail: total.costs.count > 1 ? costText(total) : "\(total.unpricedCalls) 次暂不可计价", icon: "tag", color: .orange)
        }
    }
    private var cacheSummary: some View {
        let total = totals
        return VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("缓存命中统计", systemImage: "bolt.horizontal.circle").font(.headline).foregroundStyle(.teal)
                Spacer()
                Text("服务商输入缓存").font(.caption).foregroundStyle(.secondary)
            }
            HStack(spacing: 24) {
                cacheMetric("缓存命中 Tokens", value: total.reportedCacheCalls > 0 ? total.cached.formatted() : "—")
                Divider().frame(height: 42)
                cacheMetric("未命中输入 Tokens", value: total.cacheMeasuredCalls > 0 ? total.cacheMisses.formatted() : "—")
                Divider().frame(height: 42)
                cacheMetric("缓存命中率", value: rateText(total.cacheHitRate))
            }
            if let rate = total.cacheHitRate { ProgressView(value: rate).tint(.teal).accessibilityLabel("输入 Token 缓存命中率") }
            Text("\(total.cacheMeasuredCalls) / \(total.calls) 次请求返回完整缓存明细。命中率 = 命中 Tokens ÷ 同批请求的输入 Tokens；缺失明细不按未命中计算。")
                .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(18).background(Color.teal.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.teal.opacity(0.16)))
    }
    private func cacheMetric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 22, weight: .semibold, design: .rounded)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    private func metric(_ title: String, value: String, detail: String, icon: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text(title).font(.caption).foregroundStyle(.secondary); Spacer(minLength: 2); Image(systemName: icon).font(.caption).foregroundStyle(color) }
            Text(value).font(.system(size: 25, weight: .semibold, design: .rounded)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.6).help(value)
            Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(2).frame(height: 26, alignment: .top)
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
    }
    private var trend: some View {
        SettingsCard(title: "Token 趋势", subtitle: "按本机日期汇总 · 最多展示最近 90 天") {
            Chart(daily) { point in
                BarMark(x: .value("日期", point.day, unit: .day), y: .value("Tokens", point.tokens), width: .ratio(0.65))
                    .foregroundStyle(by: .value("类型", point.kind))
            }
            .chartForegroundStyleScale(["输入": Color.teal, "输出": Color.indigo])
            .chartLegend(position: .top, alignment: .leading)
            .chartXScale(range: .plotDimension(padding: 20))
            .chartYAxis { AxisMarks(position: .leading) }
            .chartXAxis { AxisMarks(values: .automatic(desiredCount: 5)) { _ in AxisGridLine(); AxisValueLabel(format: .dateTime.month().day(), anchor: .top) } }
            .frame(height: 175)
        }
    }
    private var breakdown: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("用量明细").font(.headline)
                Spacer()
                Picker("明细分组", selection: $grouping) { Text("按任务").tag("tasks"); Text("按模型").tag("models"); Text("每次请求").tag("requests") }.labelsHidden().pickerStyle(.segmented).frame(width: 270)
            }
            VStack(spacing: 0) {
                HStack {
                    Text(grouping == "models" ? "模型 / 服务商" : grouping == "requests" ? "时间 / 模型" : "任务").frame(maxWidth: .infinity, alignment: .leading)
                    Text("输入").frame(width: 75, alignment: .trailing); Text("输出").frame(width: 75, alignment: .trailing)
                    Text("缓存命中").frame(width: 90, alignment: .trailing); Text("估算费用").frame(width: 110, alignment: .trailing)
                }.font(.caption).foregroundStyle(.secondary).padding(14)
                Divider()
                if grouping == "requests" {
                    ForEach(Array(events.prefix(rowLimit))) { event in requestRow(event) }
                    if events.count > rowLimit { Button("显示更多（已显示 \(rowLimit) / \(events.count)）") { rowLimit += 100 }.buttonStyle(.link).padding(14) }
                } else {
                    ForEach(groups) { group in groupRow(group) }
                }
            }.background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(.primary.opacity(0.06)))
        }
    }
    private func groupRow(_ group: UsageGroup) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 5) { Text(group.title).font(.callout.weight(.medium)).lineLimit(2).help(group.title); Text(group.subtitle).font(.caption2).foregroundStyle(.secondary) }.frame(maxWidth: .infinity, alignment: .leading)
                Text(group.totals.reportedInputCalls == 0 ? "—" : group.totals.input.formatted()).frame(width: 75, alignment: .trailing)
                Text(group.totals.reportedOutputCalls == 0 ? "—" : group.totals.output.formatted()).frame(width: 75, alignment: .trailing)
                VStack(alignment: .trailing, spacing: 5) {
                    Text(group.totals.reportedCacheCalls > 0 ? group.totals.cached.formatted() : "—")
                    Text(rateText(group.totals.cacheHitRate)).font(.caption2).foregroundStyle(.secondary)
                }.frame(width: 90, alignment: .trailing)
                VStack(alignment: .trailing, spacing: 5) {
                    Text(group.totals.costs.isEmpty ? "—" : costText(group.totals)).lineLimit(2)
                    if group.totals.unpricedCalls > 0 { Text("\(group.totals.unpricedCalls) 次未定价").font(.caption2).foregroundStyle(.secondary) }
                }.frame(width: 110, alignment: .trailing)
            }.font(.callout.monospacedDigit()).padding(14)
            Divider().padding(.horizontal, 14)
        }
    }
    private func requestRow(_ event: UsageEvent) -> some View {
        VStack(spacing: 0) {
            Button { detail = event } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(event.displayModel).font(.callout.weight(.medium)).lineLimit(1)
                        Text("\(event.sourceLabel) · \(event.phaseLabel) · 请求强度：\(event.requestedReasoningLabel)").font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                        Text("\(Date(timeIntervalSince1970: event.timestamp).formatted(date: .abbreviated, time: .standard)) · \(event.statusLabel)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Text(tokenText(event.usage.inputTokens)).frame(width: 75, alignment: .trailing)
                    Text(tokenText(event.usage.outputTokens)).frame(width: 75, alignment: .trailing)
                    VStack(alignment: .trailing, spacing: 5) {
                        Text(tokenText(event.usage.cachedInputTokens))
                        Text(rateText(event.usage.cacheHitRate)).font(.caption2).foregroundStyle(.secondary)
                    }.frame(width: 90, alignment: .trailing)
                    VStack(alignment: .trailing, spacing: 5) {
                        if let cost = event.cost(in: preferences, current: currentPrices), let price = event.price(in: preferences, current: currentPrices) { Text(moneyText(cost, currency: price.currency)) }
                        else { Text("—"); Text(event.unpricedReason(in: preferences, current: currentPrices)).font(.caption2).foregroundStyle(.secondary) }
                    }.frame(width: 110, alignment: .trailing)
                }.font(.callout.monospacedDigit()).padding(14).contentShape(Rectangle())
            }.buttonStyle(.plain)
            Divider().padding(.horizontal, 14)
        }
    }
    private func export() {
        let panel = NSSavePanel(); panel.title = "导出用量记录"; panel.nameFieldStringValue = "VideoDHelper-用量记录.csv"; panel.allowedContentTypes = [.commaSeparatedText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try UsageRepository.csv(events, preferences: preferences, currentPrices: currentPrices).write(to: url, atomically: true, encoding: .utf8); editor.show("已导出 \(events.count) 条筛选后的请求记录。") }
        catch { editor.show("导出失败，请检查目标文件夹权限。", error: true) }
    }
}

private struct UsageDetailSheet: View {
    @Environment(\.dismiss) private var dismiss
    let event: UsageEvent
    let preferences: AppPreferences
    let currentPrices: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack { Text("请求详情").font(.title2.bold()); Spacer(); Text(event.statusLabel).font(.caption).foregroundStyle(.secondary) }
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(event.displayModel).font(.headline)
                        Text("\(event.displayProvider) · \(Date(timeIntervalSince1970: event.timestamp).formatted(date: .complete, time: .standard))").font(.caption).foregroundStyle(.secondary)
                        Text(event.displayTask).font(.callout).lineLimit(3)
                    }
                    SettingsCard(title: "请求设置") {
                        row("用量来源", event.sourceLabel)
                        row("请求思考强度", event.requestedReasoningLabel)
                        Text("记录本次请求的选项，不代表服务商已确认实际执行档位；历史未记录的设置保持未知。").font(.caption).foregroundStyle(.secondary)
                    }
                    SettingsCard(title: "服务商报告的用量") {
                        row("总结阶段", event.phaseLabel)
                        row("输入 Tokens", tokenText(event.usage.inputTokens))
                        row("其中缓存命中", tokenText(event.usage.cachedInputTokens))
                        row("缓存命中率", rateText(event.usage.cacheHitRate))
                        Divider()
                        row("输出 Tokens", tokenText(event.usage.outputTokens))
                        row("其中推理", tokenText(event.usage.reasoningTokens))
                        Divider(); row("总 Tokens", tokenText(event.usage.totalTokens))
                        if let seconds = event.durationSeconds { row("请求耗时", String(format: "%.2f 秒", seconds)) }
                    }
                    SettingsCard(title: currentPrices ? "按当前价格预估" : "按调用时价格估算") {
                        if let price = event.price(in: preferences, current: currentPrices) {
                            row("输入 / 百万", price.inputPerMillion.map { moneyText($0, currency: price.currency) } ?? "未配置")
                            row("输出 / 百万", price.outputPerMillion.map { moneyText($0, currency: price.currency) } ?? "未配置")
                            row("缓存输入 / 百万", price.cachedInputPerMillion.map { moneyText($0, currency: price.currency) } ?? "按普通输入单价")
                            Divider()
                            row("本次估算", event.cost(in: preferences, current: currentPrices).map { moneyText($0, currency: price.currency) } ?? event.unpricedReason(in: preferences, current: currentPrices))
                        } else { Text("此记录没有可用的价格表，费用未知。").font(.callout).foregroundStyle(.secondary) }
                    }
                    Text("“—” 表示服务商未返回该字段。缓存属于输入、推理属于输出，均不会再次累加计费。").font(.caption).foregroundStyle(.secondary)
                }.padding(.trailing, 6)
            }
            HStack { Spacer(); Button("完成") { dismiss() }.buttonStyle(.borderedProminent) }
        }.padding(28).frame(width: 540, height: 700)
    }
    private func row(_ label: String, _ value: String) -> some View {
        HStack { Text(label).foregroundStyle(.secondary); Spacer(); Text(value).monospacedDigit().textSelection(.enabled) }.font(.callout)
    }
}
