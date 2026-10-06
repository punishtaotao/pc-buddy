import SwiftUI
import PhotosUI

struct ChatView: View {
    @EnvironmentObject private var store: Store

    @State private var text = ""
    @State private var selected: [UIImage] = []
    @State private var showCamera = false
    @State private var showSettings = false
    @State private var showStopConfirm = false
    @State private var pickedItems: [PhotosPickerItem] = []

    private var canSend: Bool {
        !(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && selected.isEmpty)
    }

    private var dotColor: Color {
        if store.busy { return .orange }
        return store.status == "已连接" ? .green : .red
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                log
                if !selected.isEmpty { thumbnails }
                inputBar
            }
            .navigationTitle("电脑管家")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    HStack(spacing: 6) {
                        Circle().fill(dotColor).frame(width: 8, height: 8)
                        Text(store.status).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        showStopConfirm = true
                    } label: {
                        Text("急停").font(.caption).bold()
                    }
                    .disabled(!store.busy)
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showCamera) {
                CameraPicker { img in selected.append(img) }
                    .ignoresSafeArea()
            }
            .sheet(isPresented: $showSettings) {
                SettingsView()
            }
            .confirmationDialog("确定要急停吗？", isPresented: $showStopConfirm, titleVisibility: .visible) {
                Button("急停", role: .destructive) { store.stop() }
                Button("取消", role: .cancel) {}
            }
            .alert("⚠️ 危险操作确认",
                   isPresented: Binding(
                        get: { store.pendingConfirm != nil },
                        set: { if !$0 { store.pendingConfirm = nil } }
                   ),
                   presenting: store.pendingConfirm) { _ in
                Button("拒绝", role: .cancel) { store.resolveConfirm(false) }
                Button("允许执行", role: .destructive) { store.resolveConfirm(true) }
            } message: { ev in
                Text(ev.detail ?? "")
            }
        }
    }

    // MARK: 执行记录

    private var log: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    if store.items.isEmpty {
                        Text("说句话，或者拍张照发过来。\n比如「打开记事本写一句下午三点开会」。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 12)
                    }
                    ForEach(store.items) { item in
                        ChatRow(item: item)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
            }
            .onChange(of: store.items.count) { _ in
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
            }
        }
    }

    // MARK: 待发送的图片

    private var thumbnails: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(selected.enumerated()), id: \.offset) { idx, img in
                    ZStack(alignment: .topTrailing) {
                        Image(uiImage: img)
                            .resizable()
                            .scaledToFill()
                            .frame(width: 62, height: 62)
                            .clipShape(RoundedRectangle(cornerRadius: 9))
                        Button {
                            selected.remove(at: idx)
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 17))
                                .foregroundStyle(.white, .red)
                        }
                        .offset(x: 5, y: -5)
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
        }
    }

    // MARK: 输入栏

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            PhotosPicker(selection: $pickedItems, maxSelectionCount: 4, matching: .images) {
                Image(systemName: "photo.on.rectangle")
                    .font(.system(size: 20))
                    .frame(width: 34, height: 34)
            }
            .onChange(of: pickedItems) { newItems in
                guard !newItems.isEmpty else { return }
                Task {
                    var loaded: [UIImage] = []
                    for item in newItems {
                        if let data = try? await item.loadTransferable(type: Data.self),
                           let img = UIImage(data: data) {
                            loaded.append(img)
                        }
                    }
                    await MainActor.run {
                        selected.append(contentsOf: loaded)
                        pickedItems = []
                    }
                }
            }

            Button {
                showCamera = true
            } label: {
                Image(systemName: "camera")
                    .font(.system(size: 20))
                    .frame(width: 34, height: 34)
            }

            TextField("让电脑做点什么…", text: $text, axis: .vertical)
                .lineLimit(1...5)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 12))

            Button {
                let t = text
                let imgs = selected
                text = ""
                selected = []
                store.send(text: t, images: imgs)
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 30))
            }
            .disabled(!canSend || store.busy)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

// MARK: - 单行渲染

struct ChatRow: View {
    let item: ChatItem

    @ViewBuilder
    var body: some View {
        switch item.kind {
        case .user:
            HStack {
                Spacer(minLength: 40)
                VStack(alignment: .trailing, spacing: 6) {
                    if !item.text.isEmpty {
                        Text(item.text)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(Color.accentColor)
                            .foregroundStyle(.white)
                            .clipShape(RoundedRectangle(cornerRadius: 14))
                    }
                    ForEach(Array(item.images.enumerated()), id: \.offset) { _, img in
                        Image(uiImage: img)
                            .resizable()
                            .scaledToFit()
                            .frame(maxWidth: 190)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                    }
                }
            }

        case .assistant:
            HStack {
                Text(item.text)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                Spacer(minLength: 24)
            }

        case .action:
            HStack(alignment: .top, spacing: 6) {
                Image(systemName: "wrench.and.screwdriver.fill")
                    .font(.system(size: 10))
                    .foregroundStyle(.blue)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.name)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.blue)
                    Text(item.text)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(Color(.tertiarySystemBackground))
            .overlay(alignment: .leading) {
                Rectangle().fill(Color.blue).frame(width: 3)
            }
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .padding(.trailing, 30)

        case .result:
            Text("↳ " + item.text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(item.ok ? Color.secondary : Color.red)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(.tertiarySystemBackground))
                .overlay(alignment: .leading) {
                    Rectangle().fill(item.ok ? Color.green : Color.red).frame(width: 3)
                }
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .padding(.trailing, 30)

        case .system:
            Text(item.text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)

        case .image:
            if let u = item.imageURL {
                AsyncImage(url: u) { phase in
                    switch phase {
                    case .success(let img):
                        img.resizable().scaledToFit()
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                    case .failure:
                        Text("截图加载失败").font(.caption).foregroundStyle(.secondary)
                    default:
                        ProgressView()
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.trailing, 30)
            }
        }
    }
}

// MARK: - 设置

struct SettingsView: View {
    @EnvironmentObject private var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var confirmUnpair = false

    var body: some View {
        NavigationStack {
            Form {
                if let cfg = store.config {
                    Section("连接") {
                        LabeledContent("地址", value: cfg.displayBase)
                        LabeledContent("口令", value: String(cfg.token.prefix(8)) + "…")
                        LabeledContent("状态", value: store.status)
                    }
                }
                if let info = store.info {
                    Section("电脑") {
                        LabeledContent("模型", value: info.model ?? "-")
                        LabeledContent("API Key", value: (info.hasKey ?? false) ? "已配置" : "未配置")
                        LabeledContent("看图能力", value: (info.vision ?? false) ? "已开启" : "仅本地 OCR")
                        if let wd = info.workDir {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("工作目录").font(.caption).foregroundStyle(.secondary)
                                Text(wd).font(.system(size: 11, design: .monospaced))
                            }
                        }
                    }
                }
                Section {
                    Button("断开配对", role: .destructive) { confirmUnpair = true }
                } footer: {
                    Text("断开后需要重新扫码或手动填地址才能连上。")
                }
            }
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
            .confirmationDialog("确定断开配对？", isPresented: $confirmUnpair, titleVisibility: .visible) {
                Button("断开", role: .destructive) {
                    store.unpair()
                    dismiss()
                }
                Button("取消", role: .cancel) {}
            }
        }
    }
}
