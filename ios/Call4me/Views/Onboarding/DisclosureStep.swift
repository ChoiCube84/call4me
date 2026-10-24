import SwiftUI

/// The words the AI says first on every call. Editable; {name} is filled in.
struct DisclosureStep: View {
    @EnvironmentObject private var settings: AppSettings
    let onNext: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("What the AI says first")
                    .font(.title2.bold())
                Text("The AI always says it is an AI. You can change the words.")
                    .font(.title3)
                DisclosureEditor()
                BigButton(title: "Next", systemImage: "arrow.right", action: onNext)
                    .disabled(settings.disclosureTemplate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding()
        }
    }
}

/// Shared by onboarding and Settings.
struct DisclosureEditor: View {
    @EnvironmentObject private var settings: AppSettings

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextEditor(text: $settings.disclosureTemplate)
                .font(.body)
                .frame(minHeight: 120)
                .padding(8)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityLabel("First words")
            Text("{name} becomes your name.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            Text("The company hears:")
                .font(.headline)
            Text(settings.disclosureText)
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
            Button("Use default words") {
                settings.disclosureTemplate = AppSettings.defaultDisclosure
            }
        }
    }
}
