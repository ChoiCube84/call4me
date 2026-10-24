import SwiftUI

/// Scrolling captions, newest at the bottom, each labelled with who spoke.
struct CaptionList: View {
    let captions: [Caption]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if captions.isEmpty {
                        Text("Words from the call show here.")
                            .font(.title3)
                            .foregroundStyle(.secondary)
                            .padding(.top, 40)
                    }
                    ForEach(captions) { caption in
                        CaptionRow(caption: caption).id(caption.id)
                    }
                }
                .padding()
            }
            .onChange(of: captions) { _, new in
                guard let last = new.last else { return }
                withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }
}

struct CaptionRow: View {
    let caption: Caption

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(caption.speaker.label)
                .font(.subheadline.bold())
                .foregroundStyle(color)
            Text(caption.text)
                .font(.title2)
                .foregroundStyle(caption.isFinal ? .primary : .secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
        .overlay(alignment: .leading) {
            Rectangle().fill(color).frame(width: 4)
        }
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch caption.speaker {
        case .remote: return .blue
        case .agent: return .green
        case .user: return .purple
        }
    }
}
