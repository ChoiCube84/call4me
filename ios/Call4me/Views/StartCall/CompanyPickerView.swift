import SwiftUI

/// Search the company directory (`GET /companies?q=`) or type a number.
struct CompanyPickerView: View {
    @Binding var selection: Company?
    @Environment(\.dismiss) private var dismiss

    @State private var query = ""
    @State private var results: [Company] = []
    @State private var errorText: String?
    @State private var customName = ""
    @State private var customNumber = ""

    var body: some View {
        NavigationStack {
            List {
                if let errorText {
                    Label(errorText, systemImage: "wifi.exclamationmark").foregroundStyle(.red)
                }
                Section("Companies") {
                    if results.isEmpty && errorText == nil {
                        Text("No match").foregroundStyle(.secondary)
                    }
                    ForEach(results) { company in
                        Button { choose(company) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(company.name).font(.title3)
                                if let note = company.note, !note.isEmpty {
                                    Text(note).font(.callout).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .disabled(company.phoneNumber.isEmpty)
                    }
                }
                Section("Not listed? Type it") {
                    TextField("Company name", text: $customName)
                    TextField("Phone number", text: $customNumber)
                        .keyboardType(.phonePad)
                        .textContentType(.telephoneNumber)
                    Button("Use this number") {
                        choose(Company(name: customName, phoneNumber: customNumber, note: nil))
                    }
                    .disabled(customName.isEmpty || customNumber.count < 7)
                }
            }
            .searchable(text: $query, prompt: "Search companies")
            .navigationTitle("Choose company")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
            .task(id: query) { await search() }
        }
    }

    private func search() async {
        try? await Task.sleep(nanoseconds: 250_000_000)  // debounce typing
        guard !Task.isCancelled else { return }
        do {
            results = try await APIClient().searchCompanies(query)
            errorText = nil
        } catch {
            errorText = "Can't reach the server. Check Settings."
        }
    }

    private func choose(_ company: Company) {
        Haptics.tap()
        selection = company
        dismiss()
    }
}
