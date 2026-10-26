import SwiftUI
import VideoCore

struct ModelPriceChange {
    let providerID: String
    let modelConfigID: String
    let modelID: String
    let pricing: TokenPricing
}

private struct ModelPriceDraft: Identifiable {
    let id: String
    let providerID: String
    let providerName: String
    let modelID: String
    let name: String
    var currency: String
    var input: String
    var output: String
    var cached: String

    init(provider: ModelProvider, model: LanguageModel) {
        id = model.id; providerID = provider.id; providerName = provider.name; modelID = model.modelID; name = model.name
        currency = model.pricing.currency
        input = model.pricing.inputPerMillion.map { String($0) } ?? ""
        output = model.pricing.outputPerMillion.map { String($0) } ?? ""
        cached = model.pricing.cachedInputPerMillion.map { String($0) } ?? ""
    }
    func price(unit: Int) throws -> TokenPricing {
        try TokenPricing.fromInput(currency: currency, input: input, output: output, cached: cached, tokensPerUnit: unit)
    }
}

struct ModelPricingSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [ModelPriceDraft]
    @State private var unit = 1_000_000
    @State private var error: String?
    let onSave: ([ModelPriceChange]) throws -> Void

    init(preferences: AppPreferences, onSave: @escaping ([ModelPriceChange]) throws -> Void) {
        _rows = State(initialValue: preferences.providers.flatMap { provider in provider.models.map { ModelPriceDraft(provider: provider, model: $0) } })
        self.onSave = onSave
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                Image(systemName: "tag").font(.title2).foregroundStyle(.teal)
                VStack(alignment: .leading, spacing: 5) {
                    Text("模型价格表").font(.title2.bold())
                    Text("手动填写各模型的实际单价，保存后自动换算费用。").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }
            HStack {
                Text("报价单位").font(.callout.weight(.medium))
                Picker("报价单位", selection: Binding(get: { unit }, set: changeUnit)) {
                    Text("每百万 Tokens").tag(1_000_000)
                    Text("每千 Tokens").tag(1_000)
                }.labelsHidden().frame(width: 180)
                Spacer()
                Text("切换单位会自动换算已填单价").font(.caption).foregroundStyle(.secondary)
            }
            ScrollView {
                VStack(spacing: 14) {
                    ForEach($rows) { $row in
                        SettingsCard(title: row.name, subtitle: "\(row.providerName) · \(row.modelID)") {
                            HStack(alignment: .top, spacing: 14) {
                                VStack(alignment: .leading, spacing: 7) {
                                    Text("币种").font(.caption).foregroundStyle(.secondary)
                                    Picker("\(row.name) 币种", selection: $row.currency) {
                                        Text("CNY · 人民币").tag("CNY"); Text("USD · 美元").tag("USD")
                                        Text("EUR · 欧元").tag("EUR"); Text("HKD · 港币").tag("HKD"); Text("JPY · 日元").tag("JPY")
                                    }.labelsHidden()
                                }.frame(width: 135)
                                field("输入（未命中）", text: $row.input, placeholder: "未配置", model: row.name)
                                field("输入（缓存命中）", text: $row.cached, placeholder: "同普通输入价", model: row.name)
                                field("输出", text: $row.output, placeholder: "未配置", model: row.name)
                            }
                        }
                    }
                }.padding(.trailing, 6)
            }
            Text("留空表示未配置，填 0 表示免费；缓存价格留空时按普通输入价。保存后按当前价格重估可归属的请求，调用时的价格快照保持不变。")
                .font(.caption).foregroundStyle(.secondary).lineSpacing(3)
            if let error { Text(error).font(.caption).foregroundStyle(.orange) }
            HStack {
                Text("价格表只更新单价与币种").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("保存并计算", action: save).buttonStyle(.borderedProminent).tint(.teal).keyboardShortcut("s", modifiers: .command).disabled(rows.isEmpty)
            }
        }.textFieldStyle(.roundedBorder).padding(26).frame(width: 800, height: min(660, CGFloat(max(1, rows.count) * 190 + 250)))
    }

    private func field(_ title: String, text: Binding<String>, placeholder: String, model: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: text).accessibilityLabel("\(model) \(title)单价")
        }.frame(maxWidth: .infinity)
    }
    private func changeUnit(_ newUnit: Int) {
        guard newUnit != unit else { return }
        do {
            var converted = rows
            for index in rows.indices {
                let price = try rows[index].price(unit: unit)
                func display(_ value: Double?) -> String { value.map { String(format: "%.12g", $0 * Double(newUnit) / 1_000_000) } ?? "" }
                converted[index].input = display(price.inputPerMillion)
                converted[index].output = display(price.outputPerMillion)
                converted[index].cached = display(price.cachedInputPerMillion)
            }
            rows = converted; unit = newUnit; error = nil
        } catch { self.error = error.localizedDescription }
    }
    private func save() {
        do {
            let changes = try rows.map { row in
                ModelPriceChange(providerID: row.providerID, modelConfigID: row.id, modelID: row.modelID, pricing: try row.price(unit: unit))
            }
            try onSave(changes); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}
