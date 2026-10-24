import SwiftUI

/// What the AI may and may not agree to. Enforced on the server by the guard.
struct LimitsSection: View {
    @Binding var limits: CallLimits

    var body: some View {
        Section {
            Stepper(value: $limits.maxHoldMinutes, in: 5...180, step: 5) {
                Text("Wait on hold up to \(limits.maxHoldMinutes) min")
            }
            Stepper(value: $limits.maxSpendUsd, in: 0...500, step: 5) {
                Text(limits.maxSpendUsd == 0 ? "Pay nothing" : "Pay up to $\(Int(limits.maxSpendUsd))")
            }
            Toggle("OK to change my plan", isOn: $limits.allowPlanChanges)
            Toggle("OK to cancel my service", isOn: $limits.allowCancellation)
            TextField("Other rules (optional)", text: $limits.notes, axis: .vertical)
                .lineLimit(1...4)
        } header: {
            Text("Limits")
        } footer: {
            Text("The AI will say no to anything outside these limits.")
        }
    }
}
