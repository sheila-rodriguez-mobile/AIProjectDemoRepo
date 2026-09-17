
import SwiftUI

struct ContentView: View {
    @State private var currentValue = "0"
    @State private var runningSum = 0.0
    @State private var shouldClearDisplay = false

    enum Operation {
        case add, none
    }

    let buttons: [[CalculatorButton]] = [
        [.seven, .eight, .nine],
        [.four, .five, .six],
        [.one, .two, .three],
        [.zero, .clear, .add, .equals]
    ]

    var body: some View {
        ZStack {
            Color.black.edgesIgnoringSafeArea(.all)

            VStack {
                Spacer()

                // Display
                HStack {
                    Spacer()
                    Text(currentValue)
                        .font(.system(size: 100))
                        .foregroundColor(.white)
                        .padding()
                }

                // Buttons
                ForEach(buttons, id: \.self) { row in
                    HStack(spacing: 12) {
                        ForEach(row, id: \.self) { button in
                            Button(action: {
                                self.didTap(button: button)
                            }) {
                                Text(button.rawValue)
                                    .font(.system(size: 32))
                                    .frame(
                                        width: self.buttonWidth(button: button),
                                        height: self.buttonHeight()
                                    )
                                    .background(button.buttonColor)
                                    .foregroundColor(.white)
                                    .cornerRadius(self.buttonWidth(button: button) / 2)
                            }
                        }
                    }
                    .padding(.bottom, 3)
                }
            }
        }
    }

    func didTap(button: CalculatorButton) {
        switch button {
        case .add:
            if let value = Double(currentValue) {
                runningSum = value
                shouldClearDisplay = true
            }
        case .equals:
            if let value = Double(currentValue) {
                currentValue = "\(runningSum + value)"
            }
        case .clear:
            currentValue = "0"
            runningSum = 0
        default:
            let number = button.rawValue
            if shouldClearDisplay {
                currentValue = number
                shouldClearDisplay = false
            } else {
                currentValue = currentValue == "0" ? number : currentValue + number
            }
        }
    }

    func buttonWidth(button: CalculatorButton) -> CGFloat {
        return (UIScreen.main.bounds.width - (5 * 12)) / 4
    }

    func buttonHeight() -> CGFloat {
        return (UIScreen.main.bounds.width - (5 * 12)) / 4
    }
}

enum CalculatorButton: String {
    case zero = "0", one = "1", two = "2", three = "3", four = "4", five = "5", six = "6", seven = "7", eight = "8", nine = "9"
    case equals = "=", add = "+"
    case clear = "AC"

    var buttonColor: Color {
        switch self {
        case .add, .equals:
            return .orange
        case .clear:
            return .gray
        default:
            return Color(UIColor(red: 55/255.0, green: 55/255.0, blue: 55/255.0, alpha: 1))
        }
    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
    }
}
