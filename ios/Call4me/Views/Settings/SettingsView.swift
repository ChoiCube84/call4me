import SwiftUI

/// Name, disclosure words, server address.
struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss
    @State private var testResult: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Your name") {
                    TextField("Your name", text: $settings.name)
                        .textContentType(.name)
                }
                Section("What the AI says first") {
                    DisclosureEditor()
                }
                Section {
                    TextField("http://localhost:8000", text: $settings.backendURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Access code", text: $settings.accessToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("Test connection") { Task { await test() } }
                    if let testResult { Text(testResult) }
                    Button("Use default address") {
                        settings.backendURL = AppSettings.defaultBackendURL
                        if !AppSettings.defaultAccessToken.isEmpty {
                            settings.accessToken = AppSettings.defaultAccessToken
                        }
                    }
                } header: {
                    Text("Server address")
                } footer: {
                    Text("Use the https address and access code from your call4me server. localhost only works in the Simulator.")
                }
                Section("Alerts") {
                    Button("Turn on alerts") {
                        Task { _ = await PushRegistrar.requestPermission() }
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }

    private func test() async {
        do {
            _ = try await APIClient().searchCompanies("")
            testResult = "Connected."
            Haptics.success()
        } catch APIError.badAccessCode {
            testResult = APIError.badAccessCode.errorDescription
            Haptics.error()
        } catch {
            testResult = "Can't connect. Check the address."
            Haptics.error()
        }
    }
}
