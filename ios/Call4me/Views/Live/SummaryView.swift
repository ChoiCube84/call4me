import SwiftUI
import UIKit

/// After the call: what happened, the reference number, minutes saved.
struct SummaryView: View {
    let result: CallResult
    let company: String?
    let onDone: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Label(result.failed ? "Call did not finish" : "Call done",
                      systemImage: result.failed ? "xmark.octagon.fill" : "checkmark.circle.fill")
                    .font(.largeTitle.bold())
                    .foregroundStyle(result.failed ? .red : .green)
                if let company {
                    Text(company).font(.title3).foregroundStyle(.secondary)
                }

                section("What happened") {
                    Text(result.outcome).font(.title3)
                }

                if let ref = result.referenceNumber, !ref.isEmpty {
                    section("Reference number") {
                        Text(ref)
                            .font(.largeTitle.monospaced().bold())
                            .textSelection(.enabled)
                        Button {
                            UIPasteboard.general.string = ref
                            Haptics.success()
                        } label: {
                            Label("Copy", systemImage: "doc.on.doc")
                        }
                    }
                }

                if let minutes = result.minutesSaved, minutes > 0 {
                    section("Time saved") {
                        Text("\(Int(minutes.rounded())) minutes")
                            .font(.largeTitle.bold())
                    }
                }

                BigButton(title: "Done", systemImage: "checkmark", action: onDone)
            }
            .padding()
        }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline).foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
    }
}
