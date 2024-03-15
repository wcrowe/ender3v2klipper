; *** This version is for Ender 3 size Printer ***
; Bed leveling Ender 3 by CHEP FilamentFriday.com
; For Electronic Bed Level Tool with 5.7mm offset

G90

G28 ; Home all axis
G1 Z8 ; Lift Z axis
G1 X22 Y15 F3000; Move to Position 1
G1 Z5.7
G4 P25000 ; Pause for 30 seconds
G1 Z8 ; Lift Z axis
G1 X22 Y185 F3000; Move to Position 2
G1 Z5.7
G4 P25000 ; Pause for 30 seconds
G1 Z8 ; Lift Z axis
G1 X192 Y104 F3000; Move to Position 3
G1 Z5.7
G4 P25000 ; Pause for 30 seconds


G28 X;
M84 ; disable motors



