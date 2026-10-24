import SwiftUI

/// Three short steps: your name, what the AI says first, permissions.
struct OnboardingView: View {
    @EnvironmentObject private var settings: AppSettings
    @State private var step = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ProgressView(value: Double(step + 1), total: 3)
                    .padding()
                    .accessibilityLabel("Step \(step + 1) of 3")
                Group {
                    switch step {
                    case 0: NameStep(onNext: { step = 1 })
                    case 1: DisclosureStep(onNext: { step = 2 })
                    default: PermissionsStep(onDone: { settings.hasOnboarded = true })
                    }
                }
                .frame(maxHeight: .infinity, alignment: .top)
            }
            .navigationTitle("Welcome")
            .toolbar {
                if step > 0 {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Back") { step -= 1 }
                    }
                }
            }
        }
    }
}

private struct NameStep: View {
    @EnvironmentObject private var settings: AppSettings
    let onNext: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("call4me makes phone calls for you. You read and type. The AI talks.")
                    .font(.title3)
                Text("What is your name?")
                    .font(.title2.bold())
                TextField("Your name", text: $settings.name)
                    .textContentType(.name)
                    .font(.title3)
                    .padding()
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                Text("The AI says this name to the company.")
                    .foregroundStyle(.secondary)
                BigButton(title: "Next", systemImage: "arrow.right", action: onNext)
                    .disabled(settings.name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding()
        }
    }
}
