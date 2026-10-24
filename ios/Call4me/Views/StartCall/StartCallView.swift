import SwiftUI

/// Pick a company, say what you want, set limits, start the call.
struct StartCallView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var router: AppRouter

    @State private var company: Company?
    @State private var goal = ""
    @State private var limits = CallLimits()
    @State private var isStarting = false
    @State private var errorText: String?
    @State private var showSettings = false
    @State private var showPicker = false

    var body: some View {
        Form {
            Section("Who to call") {
                Button {
                    showPicker = true
                } label: {
                    HStack {
                        Text(company?.name ?? "Choose a company")
                            .font(.title3)
                            .foregroundStyle(company == nil ? .secondary : .primary)
                        Spacer()
                        Image(systemName: "chevron.right").foregroundStyle(.secondary)
                    }
                }
                if let number = company?.phoneNumber, !number.isEmpty {
                    Text(number).foregroundStyle(.secondary)
                }
            }

            Section {
                TextField("Example: Remove the $40 late fee.", text: $goal, axis: .vertical)
                    .font(.title3)
                    .lineLimit(3...8)
            } header: {
                Text("What do you want?")
            } footer: {
                Text("Type, or tap the mic on the keyboard to talk.")
            }

            LimitsSection(limits: $limits)

            Section("The AI says first") {
                Text(settings.disclosureText).font(.callout)
            }

            Section {
                BigButton(title: isStarting ? "Starting…" : "Start call", systemImage: "phone.fill") {
                    Task { await start() }
                }
                .disabled(!canStart || isStarting)
                .listRowInsets(EdgeInsets())
                if let errorText {
                    Label(errorText, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("New call")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { showSettings = true } label: {
                    Image(systemName: "gearshape").accessibilityLabel("Settings")
                }
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .sheet(isPresented: $showPicker) { CompanyPickerView(selection: $company) }
    }

    private var canStart: Bool {
        guard let company else { return false }
        return !company.phoneNumber.isEmpty
            && !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func start() async {
        guard let company else { return }
        isStarting = true
        defer { isStarting = false }
        let body = StartCallRequest(
            userId: settings.userId,
            company: company.name,
            phoneNumber: company.phoneNumber,
            goal: goal.trimmingCharacters(in: .whitespacesAndNewlines),
            limits: limits,
            disclosureText: settings.disclosureText
        )
        do {
            let session = try await APIClient().startCall(body)
            errorText = nil
            Haptics.success()
            LiveActivityManager.shared.start(session: session)
            router.open(callId: session.id)
            goal = ""
        } catch {
            Haptics.error()
            errorText = (error as? LocalizedError)?.errorDescription ?? "Could not start the call."
        }
    }
}
