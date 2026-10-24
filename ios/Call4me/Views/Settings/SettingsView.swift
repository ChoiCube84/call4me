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
                    Button("Test connection") { Task { await test() } }
                    if let testResult { Text(testResult) }
                    Button("Use default address") {
                        settings.backendURL = AppSettings.defaultBackendURL
                    }
                } header: {
                    Text("Server address")
                } footer: {
                    Text("On a real iPhone, use your computer's address or an ngrok https URL. localhost only works in the Simulator.")
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
        } catch {
            testResult = "Can't connect. Check the address."
            Haptics.error()
        }
    }
}
