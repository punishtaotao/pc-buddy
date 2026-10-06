import SwiftUI

struct PairingView: View {
    @EnvironmentObject private var store: Store

    @State private var address = ""
    @State private var token = ""
    @State private var scanning = false
    @State private var working = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("192.168.1.100:8765", text: $address)
                        .keyboardType(.numbersAndPunctuation)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("访问口令", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("电脑地址")
                } footer: {
                    Text("在电脑上双击 start.bat，黑窗口里会显示完整网址和一个二维码。")
                }

                Section {
                    Button {
                        scanning = true
                    } label: {
                        Label("扫二维码自动填", systemImage: "qrcode.viewfinder")
                    }

                    Button {
                        Task { await connect() }
                    } label: {
                        HStack {
                            Text("连接")
                            if working {
                                Spacer()
                                ProgressView()
                            }
                        }
                    }
                    .disabled(working || address.isEmpty || token.isEmpty)
                }

                if let error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }

                Section {
                    Text("第一次连接时，iPhone 会问「是否允许访问本地网络」，必须点允许，否则连不上电脑。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("配对电脑")
            .sheet(isPresented: $scanning) {
                QRScannerView { code in
                    scanning = false
                    applyScanned(code)
                }
                .ignoresSafeArea()
            }
        }
    }

    private func connect() async {
        working = true
        error = nil
        do {
            try await store.pair(base: address, token: token)
        } catch {
            self.error = error.localizedDescription
        }
        working = false
    }

    /// 二维码里是完整的 http://ip:port/?t=xxxx
    private func applyScanned(_ code: String) {
        guard let c = URLComponents(string: code.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = c.host else {
            error = "这不是「电脑管家」的二维码"
            return
        }
        let scheme = c.scheme ?? "http"
        let port = c.port ?? 8765
        address = "\(scheme)://\(host):\(port)"
        token = c.queryItems?.first(where: { $0.name == "t" })?.value ?? ""
        error = token.isEmpty ? "二维码里没有口令，试试重新扫一次" : nil
    }
}
